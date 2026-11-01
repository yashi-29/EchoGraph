import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from conftest import fake_groq_client, fake_text_response, fake_tool_call_response
from httpx import ASGITransport, AsyncClient

from app.db.session import async_session_factory
from app.main import app
from app.models.memory_item import EMBEDDING_DIM, MemoryItem


def _basis_vector(index: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[index] = 1.0
    return vector


@asynccontextmanager
async def _signed_up_client() -> AsyncIterator[tuple[AsyncClient, uuid.UUID]]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        email = f"{uuid.uuid4()}@example.com"
        signup = await client.post(
            "/api/v1/auth/signup", json={"email": email, "password": "correct-horse-battery"}
        )
        yield client, uuid.UUID(signup.json()["id"])


async def test_chat_requires_auth() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/chat/", json={"message": "hi"})
    assert response.status_code == 401


async def test_chat_search_memory_relevance_cites_closest_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _signed_up_client() as (client, user_id):
        async with async_session_factory() as db:
            db.add_all(
                [
                    MemoryItem(
                        user_id=user_id,
                        source="gmail",
                        external_id="close-match",
                        content="Lunch with Aditya on Tuesday",
                        item_metadata={},
                        embedding=_basis_vector(0),
                    ),
                    MemoryItem(
                        user_id=user_id,
                        source="gmail",
                        external_id="far-match",
                        content="Unrelated newsletter about gardening",
                        item_metadata={},
                        embedding=_basis_vector(1),
                    ),
                ]
            )
            await db.commit()

        async def fake_embed_query(_text: str) -> list[float]:
            return _basis_vector(0)

        monkeypatch.setattr("app.services.chat.retrieval.embed_query", fake_embed_query)

        responses = [
            fake_tool_call_response(
                "search_memory", {"order": "relevance", "query": "lunch with Aditya", "limit": 1}
            ),
            fake_text_response("You had lunch with Aditya on Tuesday [1]."),
        ]
        with patch(
            "app.services.chat.groq_chat._get_client", return_value=fake_groq_client(responses)
        ):
            response = await client.post(
                "/api/v1/chat/", json={"message": "when did I have lunch with Aditya?"}
            )

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "You had lunch with Aditya on Tuesday [1]."
    assert len(body["citations"]) == 1
    assert body["citations"][0]["external_id"] == "close-match"


async def test_chat_search_memory_recent_respects_requested_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: a "give me my last 10 emails" request must not be silently
    capped at some smaller hardcoded default — the model controls limit/order/source.
    """
    async with _signed_up_client() as (client, user_id):
        now = datetime.now(UTC)
        async with async_session_factory() as db:
            db.add_all(
                [
                    MemoryItem(
                        user_id=user_id,
                        source="gmail",
                        external_id=f"msg-{i}",
                        content=f"email number {i}",
                        item_metadata={},
                        # occurred_at (actual send time) is what "recent" must sort by.
                        # created_at is deliberately the opposite order — indexing order —
                        # so this test fails if the code regresses to sorting by created_at.
                        occurred_at=now - timedelta(minutes=i),
                        created_at=now - timedelta(minutes=11 - i),
                    )
                    for i in range(12)
                ]
            )
            await db.commit()

        responses = [
            fake_tool_call_response(
                "search_memory", {"order": "recent", "source": "gmail", "limit": 10}
            ),
            fake_text_response("Here are your 10 most recent emails."),
        ]
        with patch(
            "app.services.chat.groq_chat._get_client", return_value=fake_groq_client(responses)
        ):
            response = await client.post(
                "/api/v1/chat/", json={"message": "give me my recent 10 emails"}
            )

    assert response.status_code == 200
    body = response.json()
    assert len(body["citations"]) == 10
    assert {c["external_id"] for c in body["citations"]} == {f"msg-{i}" for i in range(10)}


async def test_chat_search_memory_treats_null_arguments_as_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: models send `"source": null` / `"limit": null` meaning "not given";
    those must fall back to the defaults (all sources, default limit) instead of crashing.
    """
    async with _signed_up_client() as (client, user_id):
        async with async_session_factory() as db:
            db.add_all(
                MemoryItem(
                    user_id=user_id,
                    source=source,
                    external_id=f"{source}-1",
                    content=f"AWS certification ({source})",
                    item_metadata={},
                    embedding=_basis_vector(0),
                )
                for source in ("gmail", "document")
            )
            await db.commit()

        async def fake_embed_query(_text: str) -> list[float]:
            return _basis_vector(0)

        monkeypatch.setattr("app.services.chat.retrieval.embed_query", fake_embed_query)

        responses = [
            fake_tool_call_response(
                "search_memory",
                {"order": "relevance", "query": "certification", "source": None, "limit": None},
            ),
            fake_text_response("You have an AWS certification [1]."),
        ]
        with patch(
            "app.services.chat.groq_chat._get_client", return_value=fake_groq_client(responses)
        ):
            response = await client.post(
                "/api/v1/chat/", json={"message": "which certifications do I have?"}
            )

    assert response.status_code == 200
    assert {c["source"] for c in response.json()["citations"]} == {"gmail", "document"}
