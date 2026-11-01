import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from conftest import fake_groq_client, fake_text_response, fake_tool_call_response
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.crypto import encrypt_token
from app.db.session import async_session_factory
from app.main import app
from app.models.connector import Connector
from app.models.conversation import Conversation
from app.models.memory_item import MemoryItem
from app.models.user import User

PASSWORD = "correct-horse-battery"


@asynccontextmanager
async def _signed_up_client() -> AsyncIterator[tuple[AsyncClient, uuid.UUID, str]]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        email = f"{uuid.uuid4()}@example.com"
        signup = await client.post(
            "/api/v1/auth/signup", json={"email": email, "password": PASSWORD}
        )
        yield client, uuid.UUID(signup.json()["id"]), email


async def _seed(user_id: uuid.UUID) -> None:
    now = datetime.now(UTC)
    async with async_session_factory() as db:
        db.add_all(
            [
                MemoryItem(
                    user_id=user_id,
                    source="gmail",
                    external_id="recent",
                    content="Recent email\n\nSecret body text",
                    item_metadata={},
                    occurred_at=now - timedelta(days=10),
                ),
                MemoryItem(
                    user_id=user_id,
                    source="gmail",
                    external_id="old",
                    content="Old email\n\nAncient body",
                    item_metadata={},
                    occurred_at=now - timedelta(days=200),
                ),
                MemoryItem(
                    user_id=user_id,
                    source="google_calendar",
                    external_id="event",
                    content="Standup",
                    item_metadata={},
                    occurred_at=now - timedelta(days=1),
                ),
                Connector(
                    user_id=user_id,
                    provider="gmail",
                    external_account_email="me@gmail.com",
                    access_token_encrypted=encrypt_token("access"),
                    refresh_token_encrypted=encrypt_token("refresh"),
                    scopes="",
                ),
            ]
        )
        await db.commit()


async def test_account_routes_require_auth() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/v1/account/data")).status_code == 401
        assert (await client.patch("/api/v1/account/preferences", json={})).status_code == 401


async def test_profile_name_and_email_change_requires_password_for_email() -> None:
    async with _signed_up_client() as (client, _, email):
        named = await client.patch("/api/v1/account/profile", json={"full_name": "  Yashi G  "})
        assert named.status_code == 200
        assert named.json()["full_name"] == "Yashi G"
        assert (await client.get("/api/v1/auth/me")).json()["full_name"] == "Yashi G"

        new_email = f"{uuid.uuid4()}@example.com"
        no_password = await client.patch("/api/v1/account/profile", json={"email": new_email})
        assert no_password.status_code == 403
        wrong = await client.patch(
            "/api/v1/account/profile", json={"email": new_email, "current_password": "nope"}
        )
        assert wrong.status_code == 403

        changed = await client.patch(
            "/api/v1/account/profile",
            json={"email": new_email.upper(), "current_password": PASSWORD},
        )
        assert changed.status_code == 200
        assert changed.json()["email"] == new_email
        # Name untouched when only email is sent.
        assert changed.json()["full_name"] == "Yashi G"

        client.cookies.clear()
        old_login = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert old_login.status_code == 401
        new_login = await client.post(
            "/api/v1/auth/login", json={"email": new_email, "password": PASSWORD}
        )
        assert new_login.status_code == 200


async def test_email_change_to_taken_address_conflicts() -> None:
    async with _signed_up_client() as (_, _, taken):
        pass
    async with _signed_up_client() as (client, _, _):
        response = await client.patch(
            "/api/v1/account/profile", json={"email": taken, "current_password": PASSWORD}
        )
    assert response.status_code == 409


async def test_change_password() -> None:
    async with _signed_up_client() as (client, _, email):
        wrong = await client.post(
            "/api/v1/account/password",
            json={"current_password": "nope", "new_password": "a-brand-new-secret"},
        )
        assert wrong.status_code == 403
        too_short = await client.post(
            "/api/v1/account/password", json={"current_password": PASSWORD, "new_password": "short"}
        )
        assert too_short.status_code == 422
        ok = await client.post(
            "/api/v1/account/password",
            json={"current_password": PASSWORD, "new_password": "a-brand-new-secret"},
        )
        assert ok.status_code == 204

        client.cookies.clear()
        assert (
            await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        ).status_code == 401
        assert (
            await client.post(
                "/api/v1/auth/login", json={"email": email, "password": "a-brand-new-secret"}
            )
        ).status_code == 200


async def test_preferences_partial_updates_and_validation() -> None:
    async with _signed_up_client() as (client, _, _):
        me = (await client.get("/api/v1/auth/me")).json()
        assert me["allow_content_in_chat"] is True
        assert me["response_tone"] == "balanced"
        assert me["memory_window_days"] is None

        updated = await client.patch(
            "/api/v1/account/preferences",
            json={"response_tone": "concise", "memory_window_days": 90},
        )
        assert updated.json()["response_tone"] == "concise"
        assert updated.json()["memory_window_days"] == 90

        # Omitting memory_window_days leaves it alone; explicit null means "all time".
        toggled = await client.patch(
            "/api/v1/account/preferences", json={"allow_content_in_chat": False}
        )
        assert toggled.json()["memory_window_days"] == 90
        assert toggled.json()["allow_content_in_chat"] is False
        all_time = await client.patch(
            "/api/v1/account/preferences", json={"memory_window_days": None}
        )
        assert all_time.json()["memory_window_days"] is None

        bad_tone = await client.patch("/api/v1/account/preferences", json={"response_tone": "rude"})
        bad_window = await client.patch(
            "/api/v1/account/preferences", json={"memory_window_days": 7}
        )
        assert bad_tone.status_code == 422
        assert bad_window.status_code == 422


async def test_chat_applies_tone_window_and_content_preferences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _signed_up_client() as (client, user_id, _):
        await _seed(user_id)
        await client.patch(
            "/api/v1/account/preferences",
            json={
                "response_tone": "concise",
                "memory_window_days": 90,
                "allow_content_in_chat": False,
            },
        )

        calls: list[dict] = []
        responses = [
            fake_tool_call_response("search_memory", {"order": "recent", "source": "gmail"}),
            fake_text_response("You got one recent email."),
            fake_text_response("Title"),
        ]
        inner = fake_groq_client(responses)
        original_create = inner.chat.completions.create  # type: ignore[attr-defined]

        async def recording_create(**kwargs: object) -> object:
            calls.append(kwargs)
            return await original_create(**kwargs)

        inner.chat.completions.create = recording_create  # type: ignore[attr-defined]
        with patch("app.services.chat.groq_chat._get_client", return_value=inner):
            response = await client.post("/api/v1/chat/", json={"message": "my emails?"})

    assert response.status_code == 200
    # Memory window: the 200-day-old email is excluded.
    assert [c["external_id"] for c in response.json()["citations"]] == ["recent"]

    system_prompt = calls[0]["messages"][0]["content"]
    assert "direct and concise" in system_prompt
    assert "disabled message-content access" in system_prompt
    tool_result = calls[1]["messages"][-1]["content"]
    assert "Recent email" in tool_result
    assert "Secret body text" not in tool_result


async def test_data_summary_and_export() -> None:
    async with _signed_up_client() as (client, user_id, email):
        await _seed(user_id)
        with patch(
            "app.services.chat.groq_chat._get_client",
            return_value=fake_groq_client([fake_text_response("hi"), fake_text_response("T")]),
        ):
            await client.post("/api/v1/chat/", json={"message": "hello"})

        summary = (await client.get("/api/v1/account/data")).json()
        export = await client.get("/api/v1/account/export")

    assert summary["total_memories"] == 3
    assert summary["conversations"] == 1
    assert summary["messages"] == 2
    by_source = {s["source"]: s for s in summary["sources"]}
    assert by_source["gmail"]["memories"] == 2
    assert by_source["gmail"]["connected"] is True
    assert by_source["gmail"]["account_email"] == "me@gmail.com"
    assert by_source["google_calendar"]["connected"] is False

    assert export.status_code == 200
    assert "attachment" in export.headers["content-disposition"]
    data = json.loads(export.content)
    assert data["account"]["email"] == email
    assert len(data["memories"]) == 3
    assert data["conversations"][0]["messages"][1]["content"] == "hi"
    assert data["connected_sources"][0]["provider"] == "gmail"
    raw = export.text
    assert "hashed_password" not in raw and "access" not in json.dumps(data["connected_sources"])
    assert "embedding" not in raw


async def test_forget_source_and_clear_chat_history() -> None:
    async with _signed_up_client() as (client, user_id, _):
        await _seed(user_id)
        with patch(
            "app.services.chat.groq_chat._get_client",
            return_value=fake_groq_client([fake_text_response("hi"), fake_text_response("T")]),
        ):
            await client.post("/api/v1/chat/", json={"message": "hello"})

        forgot = await client.delete("/api/v1/account/memories", params={"source": "gmail"})
        assert forgot.json() == {"deleted": 2}
        cleared = await client.delete("/api/v1/account/conversations")
        assert cleared.json() == {"deleted": 1}
        summary = (await client.get("/api/v1/account/data")).json()

    assert summary["total_memories"] == 1
    assert summary["conversations"] == 0
    # Forgetting memories doesn't disconnect the source.
    assert {s["source"]: s["connected"] for s in summary["sources"]}["gmail"] is True


async def test_delete_twin_wipes_data_and_revokes_but_keeps_login() -> None:
    revoked: list[str] = []

    async def fake_revoke(token: str) -> bool:
        revoked.append(token)
        return True

    async with _signed_up_client() as (client, user_id, email):
        await _seed(user_id)
        with patch("app.services.oauth.google.revoke_token", fake_revoke):
            wrong = await client.post("/api/v1/account/reset", json={"password": "nope"})
            assert wrong.status_code == 403
            ok = await client.post("/api/v1/account/reset", json={"password": PASSWORD})
        assert ok.status_code == 204
        assert (await client.get("/api/v1/auth/me")).status_code == 200

    assert revoked == ["refresh"]
    async with async_session_factory() as db:
        memories = await db.scalar(
            select(func.count()).select_from(MemoryItem).where(MemoryItem.user_id == user_id)
        )
        connectors = await db.scalar(
            select(func.count()).select_from(Connector).where(Connector.user_id == user_id)
        )
        assert memories == 0 and connectors == 0
        assert (await db.get(User, user_id)) is not None
        assert email


async def test_delete_account_removes_user_and_logs_out() -> None:
    async with _signed_up_client() as (client, user_id, email):
        await _seed(user_id)
        with patch(
            "app.services.chat.groq_chat._get_client",
            return_value=fake_groq_client([fake_text_response("hi"), fake_text_response("T")]),
        ):
            await client.post("/api/v1/chat/", json={"message": "hello"})

        async def fake_revoke(_token: str) -> bool:
            return True

        with patch("app.services.oauth.google.revoke_token", fake_revoke):
            wrong = await client.post("/api/v1/account/delete", json={"password": "nope"})
            assert wrong.status_code == 403
            ok = await client.post("/api/v1/account/delete", json={"password": PASSWORD})
        assert ok.status_code == 204
        assert (await client.get("/api/v1/auth/me")).status_code == 401
        relogin = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert relogin.status_code == 401

    async with async_session_factory() as db:
        assert (await db.get(User, user_id)) is None
        conversations = await db.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.user_id == user_id)
        )
        assert conversations == 0
