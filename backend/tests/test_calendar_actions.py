import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from conftest import fake_text_response, fake_tool_call_response
from httpx import ASGITransport, AsyncClient

from app.core.crypto import encrypt_token
from app.db.session import async_session_factory
from app.main import app
from app.models.connector import Connector
from app.models.pending_action import PendingAction
from app.services.actions.google_calendar_events import build_event_payload, can_write

_WRITE_SCOPES = (
    "https://www.googleapis.com/auth/calendar.readonly "
    "https://www.googleapis.com/auth/calendar.events"
)
_READ_ONLY_SCOPES = "https://www.googleapis.com/auth/calendar.readonly"


@asynccontextmanager
async def _signed_up_client() -> AsyncIterator[tuple[AsyncClient, uuid.UUID]]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        email = f"{uuid.uuid4()}@example.com"
        signup = await client.post(
            "/api/v1/auth/signup", json={"email": email, "password": "correct-horse-battery"}
        )
        yield client, uuid.UUID(signup.json()["id"])


async def _connect(user_id: uuid.UUID, provider: str, scopes: str = "") -> None:
    async with async_session_factory() as db:
        db.add(
            Connector(
                user_id=user_id,
                provider=provider,
                access_token_encrypted=encrypt_token("fake-token"),
                scopes=scopes,
            )
        )
        await db.commit()


def _recording_groq_client(responses: list[object], calls: list[dict]) -> object:
    """Like conftest.fake_groq_client, but records each create() call's kwargs."""

    async def fake_create(**kwargs: object) -> object:
        # Snapshot the messages: the chat loop keeps appending to the same list.
        calls.append({**kwargs, "messages": list(kwargs["messages"])})  # type: ignore[call-overload]
        return responses.pop(0)

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))


def _tool_names(call: dict) -> set[str]:
    return {t["function"]["name"] for t in call.get("tools") or []}


# --- payload validation ---------------------------------------------------------------


def test_birthday_becomes_yearly_all_day_event() -> None:
    payload = build_event_payload(
        {
            "summary": "Neha's Birthday",
            "start": "2026-09-28",
            "all_day": True,
            "recurrence": "yearly",
        },
        "Asia/Kolkata",
    )
    assert payload["all_day"] is True
    assert payload["start"] == "2026-09-28"
    assert payload["end"] == "2026-09-29"  # Google's all-day end date is exclusive
    assert payload["recurrence"] == "yearly"
    assert payload["timezone"] == "Asia/Kolkata"


def test_timed_event_defaults_to_one_hour_and_drops_offset() -> None:
    payload = build_event_payload(
        {"summary": "Dentist", "start": "2026-10-02T16:00:00+05:30"}, "Asia/Kolkata"
    )
    assert payload["all_day"] is False
    assert payload["start"] == "2026-10-02T16:00:00"
    assert payload["end"] == "2026-10-02T17:00:00"


def test_multi_day_all_day_end_is_inclusive() -> None:
    payload = build_event_payload(
        {"summary": "Offsite", "start": "2026-10-12", "end": "2026-10-14", "all_day": True}, "UTC"
    )
    assert payload["end"] == "2026-10-15"  # 12th–14th inclusive → Google's exclusive 15th


def test_date_only_start_is_treated_as_all_day() -> None:
    payload = build_event_payload({"summary": "Submit report", "start": "2026-10-05"}, "UTC")
    assert payload["all_day"] is True


@pytest.mark.parametrize(
    "args",
    [
        {"start": "2026-09-28"},
        {"summary": "x"},
        {"summary": "x", "start": "not a date"},
        {"summary": "x", "start": "2026-09-28", "recurrence": "fortnightly"},
        {"summary": "x", "start": "2026-09-28T10:00", "end": "2026-09-28T09:00"},
        {"summary": "x", "start": "2026-09-28", "attendees": ["Neha"]},
    ],
)
def test_invalid_event_args_are_rejected(args: dict) -> None:
    with pytest.raises(ValueError):
        build_event_payload(args, "UTC")


def test_can_write_needs_an_events_scope() -> None:
    assert can_write(_WRITE_SCOPES)
    assert not can_write(_READ_ONLY_SCOPES)
    assert not can_write("")


# --- chat → draft ---------------------------------------------------------------------


async def test_chat_drafts_calendar_event_when_calendar_connected() -> None:
    args = {
        "summary": "Neha's Birthday",
        "start": "2026-09-28",
        "all_day": True,
        "recurrence": "yearly",
    }
    responses = [
        fake_tool_call_response("create_calendar_event", args),
        fake_text_response("I've drafted Neha's birthday — confirm below to add it."),
    ]
    calls: list[dict] = []

    async with _signed_up_client() as (client, user_id):
        await _connect(user_id, "google_calendar", _WRITE_SCOPES)
        with patch(
            "app.services.chat.groq_chat._get_client",
            return_value=_recording_groq_client(responses, calls),
        ):
            response = await client.post(
                "/api/v1/chat/",
                json={
                    "message": "in google calendar add nehas birthday on 28th sept",
                    "timezone": "Asia/Kolkata",
                },
            )

        assert response.status_code == 200
        [action] = response.json()["pending_actions"]
        assert action["tool_name"] == "create_calendar_event"
        assert action["status"] == "pending"  # nothing is written until confirm
        assert action["payload"]["end"] == "2026-09-29"
        assert action["payload"]["timezone"] == "Asia/Kolkata"

        # The model was offered the calendar tool and told the user's date + timezone.
        assert "create_calendar_event" in _tool_names(calls[0])
        system_prompt = calls[0]["messages"][0]["content"]
        assert "Asia/Kolkata" in system_prompt
        assert "Google Calendar" in system_prompt

        # The draft survives a reload of the conversation.
        detail = await client.get(
            f"/api/v1/chat/conversations/{response.json()['conversation_id']}"
        )
        twin_message = detail.json()["messages"][-1]
        assert [a["id"] for a in twin_message["pending_actions"]] == [action["id"]]


async def test_calendar_tool_not_offered_when_calendar_not_connected() -> None:
    calls: list[dict] = []
    responses = [fake_text_response("Connect Google Calendar on the Connectors page first.")]

    async with _signed_up_client() as (client, _user_id):
        with patch(
            "app.services.chat.groq_chat._get_client",
            return_value=_recording_groq_client(responses, calls),
        ):
            response = await client.post(
                "/api/v1/chat/", json={"message": "add nehas birthday on 28th sept"}
            )

    assert response.status_code == 200
    assert response.json()["pending_actions"] == []
    assert _tool_names(calls[0]) == {"search_memory"}
    assert "Not connected: Gmail, Google Calendar" in calls[0]["messages"][0]["content"]


async def test_one_message_can_draft_several_actions() -> None:
    responses = [
        fake_tool_call_response(
            "create_calendar_event", {"summary": "Neha's Birthday", "start": "2026-09-28"}
        ),
        fake_tool_call_response(
            "draft_email",
            {"to": "neha@example.com", "subject": "Happy birthday!", "body": "Have a great day"},
        ),
        fake_text_response("Drafted both — confirm below."),
    ]
    calls: list[dict] = []

    async with _signed_up_client() as (client, user_id):
        await _connect(user_id, "google_calendar", _WRITE_SCOPES)
        await _connect(user_id, "gmail")
        with patch(
            "app.services.chat.groq_chat._get_client",
            return_value=_recording_groq_client(responses, calls),
        ):
            response = await client.post(
                "/api/v1/chat/",
                json={"message": "add nehas bday on 28 sept and email her happy birthday"},
            )

    tools = [a["tool_name"] for a in response.json()["pending_actions"]]
    assert tools == ["create_calendar_event", "draft_email"]


async def test_invalid_event_args_go_back_to_the_model_not_into_a_draft() -> None:
    responses = [
        fake_tool_call_response("create_calendar_event", {"summary": "Party", "start": "soon"}),
        fake_text_response("What day is the party?"),
    ]
    calls: list[dict] = []

    async with _signed_up_client() as (client, user_id):
        await _connect(user_id, "google_calendar", _WRITE_SCOPES)
        with patch(
            "app.services.chat.groq_chat._get_client",
            return_value=_recording_groq_client(responses, calls),
        ):
            response = await client.post("/api/v1/chat/", json={"message": "add a party"})

    assert response.json()["pending_actions"] == []
    tool_result = calls[1]["messages"][-1]
    assert tool_result["role"] == "tool"
    assert "error" in tool_result["content"]


async def test_email_to_a_bare_name_is_sent_back_to_the_model() -> None:
    responses = [
        fake_tool_call_response("draft_email", {"to": "Aditya", "subject": "NOC", "body": "x"}),
        fake_text_response("What's Aditya's email address?"),
    ]
    calls: list[dict] = []

    async with _signed_up_client() as (client, user_id):
        await _connect(user_id, "gmail")
        with patch(
            "app.services.chat.groq_chat._get_client",
            return_value=_recording_groq_client(responses, calls),
        ):
            response = await client.post("/api/v1/chat/", json={"message": "mail aditya the NOC"})

    assert response.json()["pending_actions"] == []
    assert "search_memory" in calls[1]["messages"][-1]["content"]


# --- confirm --------------------------------------------------------------------------


async def _pending_event(user_id: uuid.UUID) -> uuid.UUID:
    async with async_session_factory() as db:
        action = PendingAction(
            user_id=user_id,
            tool_name="create_calendar_event",
            payload=build_event_payload(
                {"summary": "Neha's Birthday", "start": "2026-09-28", "recurrence": "yearly"},
                "Asia/Kolkata",
            ),
            status="pending",
        )
        db.add(action)
        await db.commit()
        await db.refresh(action)
        return action.id


async def test_confirm_with_read_only_calendar_asks_to_reconnect_and_stays_pending() -> None:
    async with _signed_up_client() as (client, user_id):
        await _connect(user_id, "google_calendar", _READ_ONLY_SCOPES)
        action_id = await _pending_event(user_id)

        response = await client.post(f"/api/v1/chat/actions/{action_id}/confirm")
        assert response.status_code == 400
        assert "Reconnect" in response.json()["detail"]

        still = await client.get(f"/api/v1/chat/actions/{action_id}")
        assert still.json()["status"] == "pending"


async def test_confirm_creates_the_event(monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict = {}

    async def fake_create_event(access_token: str, payload: dict) -> dict:
        created.update(access_token=access_token, payload=payload)
        return {"id": "evt-1", "html_link": "https://calendar.google.com/event?eid=evt-1"}

    async def fake_execute(tool_name: str, access_token: str, payload: dict) -> dict:
        assert tool_name == "create_calendar_event"
        return await fake_create_event(access_token, payload)

    monkeypatch.setattr("app.api.v1.endpoints.chat.google_calendar_events.execute", fake_execute)

    async with _signed_up_client() as (client, user_id):
        await _connect(user_id, "google_calendar", _WRITE_SCOPES)
        action_id = await _pending_event(user_id)

        response = await client.post(f"/api/v1/chat/actions/{action_id}/confirm")

    assert response.status_code == 200
    assert response.json()["status"] == "executed"
    assert response.json()["result"]["id"] == "evt-1"
    assert created["access_token"] == "fake-token"
    assert created["payload"]["summary"] == "Neha's Birthday"
