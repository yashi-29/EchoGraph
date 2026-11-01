import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from conftest import fake_groq_client, fake_text_response, fake_tool_call_response
from httpx import ASGITransport, AsyncClient

from app.core.crypto import encrypt_token
from app.db.session import async_session_factory
from app.main import app
from app.models.connector import Connector


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


async def test_chat_draft_email_tool_call_creates_pending_action() -> None:
    draft_args = {"to": "aditya@example.com", "subject": "NOC", "body": "Please find attached."}

    responses = [
        fake_tool_call_response("draft_email", draft_args),
        fake_text_response("I've drafted that email for you to review."),
    ]

    async with _signed_up_client() as (client, user_id):
        await _connect(user_id, "gmail")
        with patch(
            "app.services.chat.groq_chat._get_client", return_value=fake_groq_client(responses)
        ):
            response = await client.post("/api/v1/chat/", json={"message": "email Aditya my NOC"})

        assert response.status_code == 200
        body = response.json()
        assert body["reply"] == "I've drafted that email for you to review."
        [action] = body["pending_actions"]
        assert action["status"] == "pending"
        assert action["payload"] == draft_args
        action_id = action["id"]

        # Fetchable back by id.
        fetched = await client.get(f"/api/v1/chat/actions/{action_id}")
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "pending"


async def test_confirm_action_requires_gmail_connector() -> None:
    async with _signed_up_client() as (client, user_id):
        async with async_session_factory() as db:
            from app.models.pending_action import PendingAction

            action = PendingAction(
                user_id=user_id,
                tool_name="draft_email",
                payload={"to": "a@example.com", "subject": "s", "body": "b"},
                status="pending",
            )
            db.add(action)
            await db.commit()
            await db.refresh(action)
            action_id = action.id

        response = await client.post(f"/api/v1/chat/actions/{action_id}/confirm")

    assert response.status_code == 400
    assert "Gmail isn't connected" in response.json()["detail"]


async def test_confirm_action_sends_email_and_marks_executed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.pending_action import PendingAction

    async with _signed_up_client() as (client, user_id):
        async with async_session_factory() as db:
            db.add(
                Connector(
                    user_id=user_id,
                    provider="gmail",
                    access_token_encrypted=encrypt_token("fake-token"),
                    scopes="",
                )
            )
            action = PendingAction(
                user_id=user_id,
                tool_name="draft_email",
                payload={"to": "aditya@example.com", "subject": "NOC", "body": "text"},
                status="pending",
            )
            db.add(action)
            await db.commit()
            await db.refresh(action)
            action_id = action.id

        sent: dict[str, str] = {}

        async def fake_send_email(
            access_token: str, to: str, subject: str, body: str, **extra: object
        ) -> dict:
            sent.update(access_token=access_token, to=to, subject=subject, body=body)
            assert extra["attachments"] == [] and extra["thread_id"] is None
            return {"id": "msg-123"}

        monkeypatch.setattr("app.api.v1.endpoints.chat.gmail_send.send_email", fake_send_email)

        response = await client.post(f"/api/v1/chat/actions/{action_id}/confirm")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "executed"
        assert body["result"] == {"id": "msg-123"}
        assert sent["to"] == "aditya@example.com"

        # Confirming again is rejected — already executed.
        second = await client.post(f"/api/v1/chat/actions/{action_id}/confirm")
        assert second.status_code == 409


async def test_cancel_action() -> None:
    from app.models.pending_action import PendingAction

    async with _signed_up_client() as (client, user_id):
        async with async_session_factory() as db:
            action = PendingAction(
                user_id=user_id,
                tool_name="draft_email",
                payload={"to": "a@example.com", "subject": "s", "body": "b"},
                status="pending",
            )
            db.add(action)
            await db.commit()
            await db.refresh(action)
            action_id = action.id

        response = await client.post(f"/api/v1/chat/actions/{action_id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


async def test_action_from_another_user_is_not_found() -> None:
    from app.models.pending_action import PendingAction

    async with (
        _signed_up_client() as (_other_client, other_user_id),
        async_session_factory() as db,
    ):
        action = PendingAction(
            user_id=other_user_id,
            tool_name="draft_email",
            payload={"to": "a@example.com", "subject": "s", "body": "b"},
            status="pending",
        )
        db.add(action)
        await db.commit()
        await db.refresh(action)
        action_id = action.id

    async with _signed_up_client() as (client, _my_user_id):
        response = await client.get(f"/api/v1/chat/actions/{action_id}")

    assert response.status_code == 404


async def test_confirm_with_expired_google_access_keeps_the_draft_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.pending_action import PendingAction
    from app.services.oauth.tokens import ConnectorAccessExpired

    async with _signed_up_client() as (client, user_id):
        await _connect(user_id, "gmail")
        async with async_session_factory() as db:
            action = PendingAction(
                user_id=user_id,
                tool_name="draft_email",
                payload={"to": "aditya@example.com", "subject": "NOC", "body": "text"},
                status="pending",
            )
            db.add(action)
            await db.commit()
            action_id = action.id

        async def expired(_connector: object, _db: object) -> str:
            raise ConnectorAccessExpired("gmail")

        monkeypatch.setattr("app.api.v1.endpoints.chat.get_valid_access_token", expired)
        response = await client.post(f"/api/v1/chat/actions/{action_id}/confirm")

        assert response.status_code == 400
        assert "reconnect Gmail on the Connectors page" in response.json()["detail"]
        # Still confirmable once the user has reconnected.
        fetched = await client.get(f"/api/v1/chat/actions/{action_id}")
        assert fetched.json()["status"] == "pending"


def test_leaked_draft_notes_are_stripped_from_replies() -> None:
    from app.api.v1.endpoints.chat import strip_draft_notes

    leaked = (
        "I've drafted an email to shinzo2912@gmail.com. Please review and confirm the draft "
        'below.\n\n[draft_email draft, status=executed: {"to": "shinzo2912@gmail.com", '
        '"subject": "Thanks", "body": "Hi Shinzo,\n\nThank you.", "cc": ["y@gmail.com"]}]'
    )
    assert strip_draft_notes(leaked) == (
        "I've drafted an email to shinzo2912@gmail.com. Please review and confirm the draft below."
    )
    assert strip_draft_notes("No drafts [1] here.") == "No drafts [1] here."


async def test_history_replays_drafts_as_a_separate_context_note() -> None:
    from app.api.v1.endpoints.chat import _load_history
    from app.models.conversation import Conversation, ConversationMessage
    from app.models.pending_action import PendingAction

    async with _signed_up_client() as (_client, user_id), async_session_factory() as db:
        conversation = Conversation(user_id=user_id, title="t")
        db.add(conversation)
        await db.flush()
        db.add(ConversationMessage(conversation_id=conversation.id, role="user", content="hi"))
        await db.flush()
        reply = ConversationMessage(
            conversation_id=conversation.id, role="assistant", content="Drafted it."
        )
        db.add(reply)
        await db.flush()
        db.add(
            PendingAction(
                user_id=user_id,
                tool_name="draft_email",
                payload={"to": "a@example.com", "subject": "S", "body": "B"},
                status="pending",
                message_id=reply.id,
            )
        )
        await db.commit()
        history = await _load_history(conversation.id, db)

    assert [m["role"] for m in history] == ["user", "assistant", "system"]
    assert history[1]["content"] == "Drafted it."  # the reply itself stays clean
    assert "draft_email (pending)" in history[2]["content"]
    assert "Never write this list" in history[2]["content"]
