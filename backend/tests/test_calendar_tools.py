"""Live calendar tools (list/free-time) and the edit / delete / RSVP drafts — Google mocked."""

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from conftest import fake_text_response, fake_tool_call_response
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.crypto import encrypt_token
from app.db.session import async_session_factory
from app.main import app
from app.models.connector import Connector
from app.models.memory_item import MemoryItem
from app.models.pending_action import PendingAction
from app.services.actions import google_calendar_events as gcal
from app.services.chat import calendar_tools

TZ = "Asia/Kolkata"
_WRITE_SCOPES = (
    "https://www.googleapis.com/auth/calendar.readonly "
    "https://www.googleapis.com/auth/calendar.events"
)
_CALENDARS = [
    {"id": "primary-id", "name": "Yashi", "primary": True, "access": "owner", "shown": True},
    {"id": "work-id", "name": "Work", "primary": False, "access": "owner", "shown": True},
    {"id": "hidden-id", "name": "Holidays", "primary": False, "access": "reader", "shown": False},
]


def _timed(event_id: str, start: str, end: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": event_id,
        "summary": extra.pop("summary", "Dentist"),
        "start": {"dateTime": f"{start}:00+05:30"},
        "end": {"dateTime": f"{end}:00+05:30"},
        **extra,
    }


def _all_day(event_id: str, start: str, end: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": event_id,
        "summary": extra.pop("summary", "Offsite"),
        "start": {"date": start},
        "end": {"date": end},
        **extra,
    }


def _session() -> calendar_tools.CalendarSession:
    session = calendar_tools.CalendarSession(uuid.uuid4(), None, TZ)  # type: ignore[arg-type]
    session._token = "fake-token"
    session._calendars = _CALENDARS
    return session


# --- update drafts --------------------------------------------------------------------


def test_moving_a_timed_event_to_another_day_keeps_time_and_length() -> None:
    event = _timed("e1", "2026-10-02T16:00", "2026-10-02T16:45")
    payload = gcal.build_update_payload(event, None, {"start": "2026-10-05"}, "primary", TZ)
    assert payload["after"]["start"] == "2026-10-05T16:00"
    assert payload["after"]["end"] == "2026-10-05T16:45"
    assert payload["patch"]["start"]["dateTime"] == "2026-10-05T16:00:00"
    assert payload["before"]["start"] == "2026-10-02T16:00"


def test_moving_an_all_day_event_keeps_its_length() -> None:
    event = _all_day("e1", "2026-10-12", "2026-10-15")  # 12th–14th
    payload = gcal.build_update_payload(event, None, {"start": "2026-10-19"}, "primary", TZ)
    assert payload["patch"]["start"]["date"] == "2026-10-19"
    assert payload["patch"]["end"]["date"] == "2026-10-22"


def test_series_edit_changes_only_time_of_day() -> None:
    occurrence = _timed(
        "s1_20261005", "2026-10-05T10:00", "2026-10-05T10:15", recurringEventId="s1"
    )
    master = _timed("s1", "2026-09-07T10:00", "2026-09-07T10:15", recurrence=["RRULE:FREQ=WEEKLY"])
    payload = gcal.build_update_payload(
        occurrence, master, {"start": "2026-10-05T11:00", "apply_to": "series"}, "primary", TZ
    )
    assert payload["event_id"] == "s1"
    assert payload["apply_to"] == "series"
    assert payload["patch"]["start"]["dateTime"] == "2026-09-07T11:00:00"
    assert payload["patch"]["end"]["dateTime"] == "2026-09-07T11:15:00"


def test_update_rename_reminders_and_guests() -> None:
    event = _timed(
        "e1",
        "2026-10-02T16:00",
        "2026-10-02T17:00",
        attendees=[{"email": "me@example.com", "self": True}, {"email": "a@example.com"}],
    )
    payload = gcal.build_update_payload(
        event,
        None,
        {
            "summary": "Dentist (Dr. Rao)",
            "reminder_minutes": [60, 1440],
            "add_attendees": ["b@example.com"],
            "remove_attendees": ["a@example.com"],
        },
        "primary",
        TZ,
    )
    patch_body = payload["patch"]
    assert patch_body["summary"] == "Dentist (Dr. Rao)"
    assert patch_body["reminders"] == {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": 60}, {"method": "popup", "minutes": 1440}],
    }
    assert [a["email"] for a in patch_body["attendees"]] == ["me@example.com", "b@example.com"]
    assert "start" not in patch_body
    assert payload["send_updates"] == "all"


@pytest.mark.parametrize(
    "args",
    [
        {},  # nothing to change
        {"recurrence": "weekly"},  # repeat rule on a single occurrence needs apply_to=series
        {"add_attendees": ["Neha"]},
        {"reminder_minutes": [99999]},
    ],
)
def test_invalid_updates_are_rejected(args: dict) -> None:
    event = _timed("e1", "2026-10-02T16:00", "2026-10-02T17:00", recurringEventId="s1")
    with pytest.raises(ValueError):
        gcal.build_update_payload(event, None, args, "primary", TZ)


def test_rsvp_needs_the_user_to_be_a_guest() -> None:
    not_invited = _timed("e1", "2026-10-02T16:00", "2026-10-02T17:00")
    with pytest.raises(ValueError):
        gcal.build_rsvp_payload(not_invited, None, "accepted", "primary", TZ)

    invited = _timed(
        "e1",
        "2026-10-02T16:00",
        "2026-10-02T17:00",
        attendees=[{"email": "me@example.com", "self": True, "responseStatus": "needsAction"}],
        organizer={"email": "boss@example.com"},
    )
    payload = gcal.build_rsvp_payload(invited, None, "declined", "primary", TZ)
    assert payload["response"] == "declined"
    assert payload["event"]["organizer"] == "boss@example.com"
    assert payload["event"]["my_response"] == "needsAction"


def test_create_with_reminders() -> None:
    payload = gcal.build_event_payload(
        {"summary": "Flight", "start": "2026-10-10T06:00", "reminder_minutes": [180]}, TZ
    )
    assert gcal._create_body(payload)["reminders"]["overrides"] == [
        {"method": "popup", "minutes": 180}
    ]


# --- read tools -----------------------------------------------------------------------


async def test_list_events_merges_shown_calendars_and_makes_all_day_ends_inclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def fake_list_events(token, calendar_id, time_min, time_max, tz, query=None, limit=50):
        seen.append(calendar_id)
        if calendar_id == "work-id":
            return [_timed("w1", "2026-10-02T09:00", "2026-10-02T10:00", summary="Standup")]
        return [_all_day("p1", "2026-10-01", "2026-10-04", summary="Offsite")]

    monkeypatch.setattr(gcal, "list_events", fake_list_events)
    result = json.loads(
        await calendar_tools.run_read_tool(
            "list_calendar_events",
            {"time_min": "2026-10-01", "time_max": "2026-10-07"},
            _session(),
        )
    )
    assert seen == ["primary-id", "work-id"]  # the hidden calendar is skipped
    assert [e["summary"] for e in result["events"]] == ["Offsite", "Standup"]
    assert result["events"][0]["end"] == "2026-10-03"  # last day, inclusive
    assert result["events"][1]["calendar"] == "Work"


async def test_list_events_by_calendar_name(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def fake_list_events(token, calendar_id, *args: Any, **kwargs: Any):
        seen.append(calendar_id)
        return []

    monkeypatch.setattr(gcal, "list_events", fake_list_events)
    await calendar_tools.run_read_tool(
        "list_calendar_events", {"calendar_id": "work", "query": "review"}, _session()
    )
    assert seen == ["work-id"]

    unknown = json.loads(
        await calendar_tools.run_read_tool(
            "list_calendar_events", {"calendar_id": "gym"}, _session()
        )
    )
    assert unknown["status"] == "error"
    assert "Work" in unknown["message"]


async def test_find_free_time_subtracts_busy_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    tz = ZoneInfo(TZ)

    async def fake_busy(token, calendar_ids, time_min, time_max, tz_name):
        assert calendar_ids == ["primary-id", "work-id"]
        return [
            (datetime(2030, 1, 7, 10, 0, tzinfo=tz), datetime(2030, 1, 7, 11, 0, tzinfo=tz)),
            (datetime(2030, 1, 7, 10, 30, tzinfo=tz), datetime(2030, 1, 7, 12, 0, tzinfo=tz)),
            (datetime(2030, 1, 7, 16, 0, tzinfo=tz), datetime(2030, 1, 7, 17, 45, tzinfo=tz)),
        ]

    monkeypatch.setattr(gcal, "busy_blocks", fake_busy)
    result = json.loads(
        await calendar_tools.run_read_tool(
            "find_free_time",
            {"time_min": "2030-01-07", "duration_minutes": 30},  # a Monday
            _session(),
        )
    )
    slots = [(s["from"][11:], s["to"][11:]) for s in result["free_slots"]]
    # 17:45–18:00 is only 15 minutes, so it's dropped.
    assert slots == [("09:00", "10:00"), ("12:00", "16:00")]


async def test_find_free_time_skips_weekends_unless_only_weekend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_busy(*args: Any) -> list:
        return []

    monkeypatch.setattr(gcal, "busy_blocks", no_busy)
    week = json.loads(
        await calendar_tools.run_read_tool(
            "find_free_time", {"time_min": "2030-01-11", "time_max": "2030-01-14"}, _session()
        )
    )
    assert [s["day"][:3] for s in week["free_slots"]] == ["Fri", "Mon"]

    weekend = json.loads(
        await calendar_tools.run_read_tool(
            "find_free_time", {"time_min": "2030-01-12", "time_max": "2030-01-13"}, _session()
        )
    )
    assert [s["day"][:3] for s in weekend["free_slots"]] == ["Sat", "Sun"]


# --- through the chat endpoint --------------------------------------------------------


@asynccontextmanager
async def _calendar_user(
    scopes: str = _WRITE_SCOPES,
) -> AsyncIterator[tuple[AsyncClient, uuid.UUID]]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        signup = await client.post(
            "/api/v1/auth/signup",
            json={"email": f"{uuid.uuid4()}@example.com", "password": "correct-horse-battery"},
        )
        user_id = uuid.UUID(signup.json()["id"])
        async with async_session_factory() as db:
            db.add(
                Connector(
                    user_id=user_id,
                    provider="google_calendar",
                    access_token_encrypted=encrypt_token("fake-token"),
                    scopes=scopes,
                )
            )
            await db.commit()
        yield client, user_id


def _groq(responses: list[object], calls: list[dict]) -> object:
    async def fake_create(**kwargs: object) -> object:
        # Snapshot the messages: the chat loop keeps appending to the same list.
        calls.append({**kwargs, "messages": list(kwargs["messages"])})  # type: ignore[call-overload]
        return responses.pop(0)

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))


async def test_chat_find_then_move_event(monkeypatch: pytest.MonkeyPatch) -> None:
    dentist = _timed("d1", "2026-10-02T16:00", "2026-10-02T17:00")

    async def fake_list_calendars(token: str) -> list:
        return [_CALENDARS[0]]

    async def fake_list_events(*args: Any, **kwargs: Any) -> list:
        return [dentist]

    async def fake_get_event(token: str, calendar_id: str, event_id: str, tz: str) -> dict:
        assert (calendar_id, event_id) == ("primary-id", "d1")
        return dentist

    monkeypatch.setattr(gcal, "list_calendars", fake_list_calendars)
    monkeypatch.setattr(gcal, "list_events", fake_list_events)
    monkeypatch.setattr(gcal, "get_event", fake_get_event)

    responses = [
        fake_tool_call_response("list_calendar_events", {"query": "dentist"}),
        fake_tool_call_response(
            "update_calendar_event",
            {
                "event_id": "d1",
                "calendar_id": "primary-id",
                "start": "2026-10-05",
                "apply_to": None,
            },
        ),
        fake_text_response("I've drafted moving your dentist visit to Monday."),
    ]
    calls: list[dict] = []
    async with _calendar_user() as (client, _user_id):
        with patch("app.services.chat.groq_chat._get_client", return_value=_groq(responses, calls)):
            response = await client.post(
                "/api/v1/chat/",
                json={"message": "move my dentist to monday", "timezone": TZ},
            )

    assert response.status_code == 200
    listed = json.loads(calls[1]["messages"][-1]["content"])
    assert listed["events"][0]["id"] == "d1"
    [action] = response.json()["pending_actions"]
    assert action["tool_name"] == "update_calendar_event"
    assert action["status"] == "pending"
    assert action["payload"]["after"]["start"] == "2026-10-05T16:00"


async def test_confirm_delete_calls_google_and_forgets_the_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[tuple[str, dict]] = []

    async def fake_execute(tool_name: str, token: str, payload: dict) -> dict:
        executed.append((tool_name, payload))
        return {"id": payload["event_id"], "deleted": True}

    monkeypatch.setattr("app.api.v1.endpoints.chat.google_calendar_events.execute", fake_execute)

    async with _calendar_user() as (client, user_id):
        async with async_session_factory() as db:
            db.add(
                MemoryItem(
                    user_id=user_id,
                    source="google_calendar",
                    external_id="d1",
                    content="Dentist\n\n",
                    item_metadata={},
                )
            )
            action = PendingAction(
                user_id=user_id,
                tool_name="delete_calendar_event",
                payload=gcal.build_delete_payload(
                    _timed("d1", "2026-10-02T16:00", "2026-10-02T17:00"), None, "primary", TZ
                ),
                status="pending",
            )
            db.add(action)
            await db.commit()
            action_id = action.id

        response = await client.post(f"/api/v1/chat/actions/{action_id}/confirm")
        assert response.status_code == 200
        assert response.json()["status"] == "executed"
        assert executed[0][0] == "delete_calendar_event"

        async with async_session_factory() as db:
            remaining = await db.execute(
                select(MemoryItem).where(
                    MemoryItem.user_id == user_id, MemoryItem.external_id == "d1"
                )
            )
            assert remaining.scalar_one_or_none() is None


async def test_read_only_calendar_still_drafts_but_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        fake_tool_call_response("create_calendar_event", {"summary": "Gym", "start": "2026-10-01"}),
        fake_text_response("Drafted — reconnect Calendar first."),
    ]
    calls: list[dict] = []
    async with _calendar_user(scopes="https://www.googleapis.com/auth/calendar.readonly") as (
        client,
        _user_id,
    ):
        with patch("app.services.chat.groq_chat._get_client", return_value=_groq(responses, calls)):
            response = await client.post("/api/v1/chat/", json={"message": "add gym oct 1"})

    assert len(response.json()["pending_actions"]) == 1
    assert "read-only" in calls[1]["messages"][-1]["content"]


async def test_all_calendar_tools_offered_when_connected() -> None:
    calls: list[dict] = []
    async with _calendar_user() as (client, _user_id):
        with patch(
            "app.services.chat.groq_chat._get_client",
            return_value=_groq([fake_text_response("hi")], calls),
        ):
            await client.post("/api/v1/chat/", json={"message": "hi"})

    offered = {t["function"]["name"] for t in calls[0]["tools"]}
    assert offered == {"search_memory", *calendar_tools.ALL_TOOLS}
