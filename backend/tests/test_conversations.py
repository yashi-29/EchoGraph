import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

from conftest import fake_groq_client, fake_text_response
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.chat.groq_chat import fallback_title


@asynccontextmanager
async def _signed_up_client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/v1/auth/signup",
            json={"email": f"{uuid.uuid4()}@example.com", "password": "correct-horse-battery"},
        )
        yield client


def _recording_groq_client(responses: list[object], calls: list[dict]) -> object:
    async def fake_create(**kwargs: object) -> object:
        calls.append(kwargs)
        return responses.pop(0)

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))


async def test_conversation_endpoints_require_auth() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/chat/conversations")
    assert response.status_code == 401


async def test_first_message_creates_titled_conversation_and_persists_both_turns() -> None:
    async with _signed_up_client() as client:
        responses = [fake_text_response("Hi! Ask me anything."), fake_text_response("Greetings")]
        with patch(
            "app.services.chat.groq_chat._get_client", return_value=fake_groq_client(responses)
        ):
            sent = await client.post("/api/v1/chat/", json={"message": "hello twin"})

        assert sent.status_code == 200
        body = sent.json()
        assert body["conversation_title"] == "Greetings"
        conversation_id = body["conversation_id"]

        listing = await client.get("/api/v1/chat/conversations")
        assert listing.status_code == 200
        [summary] = listing.json()
        assert summary["id"] == conversation_id
        assert summary["title"] == "Greetings"
        assert summary["preview"] == "Hi! Ask me anything."

        detail = await client.get(f"/api/v1/chat/conversations/{conversation_id}")
        assert detail.status_code == 200
        messages = detail.json()["messages"]
        assert [(m["role"], m["content"]) for m in messages] == [
            ("user", "hello twin"),
            ("assistant", "Hi! Ask me anything."),
        ]
        assert messages[1]["id"] == body["message_id"]


async def test_follow_up_replays_server_side_history_and_keeps_title() -> None:
    async with _signed_up_client() as client:
        with patch(
            "app.services.chat.groq_chat._get_client",
            return_value=fake_groq_client(
                [fake_text_response("First answer"), fake_text_response("Thread title")]
            ),
        ):
            first = await client.post("/api/v1/chat/", json={"message": "first question"})
        conversation_id = first.json()["conversation_id"]

        calls: list[dict] = []
        with patch(
            "app.services.chat.groq_chat._get_client",
            return_value=_recording_groq_client([fake_text_response("Second answer")], calls),
        ):
            second = await client.post(
                "/api/v1/chat/",
                json={"message": "follow up", "conversation_id": conversation_id},
            )

        assert second.status_code == 200
        assert second.json()["conversation_title"] == "Thread title"
        # Only one model call: follow-ups don't regenerate the title.
        assert len(calls) == 1
        sent_messages = [(m["role"], m["content"]) for m in calls[0]["messages"][1:]]
        assert sent_messages == [
            ("user", "first question"),
            ("assistant", "First answer"),
            ("user", "follow up"),
        ]

        detail = await client.get(f"/api/v1/chat/conversations/{conversation_id}")
        assert len(detail.json()["messages"]) == 4


async def test_title_falls_back_to_opening_words_when_generation_fails() -> None:
    message = "what did I agree to in the budget meeting with finance last Thursday afternoon"
    async with _signed_up_client() as client:
        # Only the reply is queued, so the title call fails (empty queue) and must fall back.
        with patch(
            "app.services.chat.groq_chat._get_client",
            return_value=fake_groq_client([fake_text_response("You agreed to cut 5%.")]),
        ):
            response = await client.post("/api/v1/chat/", json={"message": message})

    assert response.status_code == 200
    assert response.json()["conversation_title"] == fallback_title(message)
    assert response.json()["conversation_title"].endswith("…")


async def test_rename_and_delete_conversation() -> None:
    async with _signed_up_client() as client:
        with patch(
            "app.services.chat.groq_chat._get_client",
            return_value=fake_groq_client([fake_text_response("ok"), fake_text_response("T")]),
        ):
            sent = await client.post("/api/v1/chat/", json={"message": "hi"})
        conversation_id = sent.json()["conversation_id"]

        renamed = await client.patch(
            f"/api/v1/chat/conversations/{conversation_id}", json={"title": "Budget notes"}
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "Budget notes"

        deleted = await client.delete(f"/api/v1/chat/conversations/{conversation_id}")
        assert deleted.status_code == 204
        assert (await client.get("/api/v1/chat/conversations")).json() == []
        missing = await client.get(f"/api/v1/chat/conversations/{conversation_id}")
        assert missing.status_code == 404


async def test_other_users_conversation_is_not_found() -> None:
    async with _signed_up_client() as owner:
        with patch(
            "app.services.chat.groq_chat._get_client",
            return_value=fake_groq_client([fake_text_response("ok"), fake_text_response("T")]),
        ):
            sent = await owner.post("/api/v1/chat/", json={"message": "private"})
        conversation_id = sent.json()["conversation_id"]

    async with _signed_up_client() as intruder:
        url = f"/api/v1/chat/conversations/{conversation_id}"
        assert (await intruder.get(url)).status_code == 404
        assert (await intruder.patch(url, json={"title": "x"})).status_code == 404
        assert (await intruder.delete(url)).status_code == 404
        posted = await intruder.post(
            "/api/v1/chat/", json={"message": "hi", "conversation_id": conversation_id}
        )
        assert posted.status_code == 404
        assert (await intruder.get("/api/v1/chat/conversations")).json() == []
