import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

import app.services.ingestion.sync as sync_module
import app.services.oauth.tokens as tokens_module
from app.core.crypto import decrypt_token, encrypt_token
from app.db.session import async_session_factory
from app.models.connector import Connector
from app.models.memory_item import EMBEDDING_DIM, MemoryItem
from app.models.user import User
from app.services.ingestion.base import NormalizedItem, SyncBatch
from app.services.oauth.google import GoogleTokenResponse
from app.workers.tasks import connector_sync as connector_sync_task


async def _create_connector(provider: str, *, expired: bool = False) -> Connector:
    async with async_session_factory() as db:
        user = User(email=f"{uuid.uuid4()}@example.com", hashed_password="irrelevant-for-this-test")
        db.add(user)
        await db.flush()

        expires_at = datetime.now(UTC) + (timedelta(minutes=-5) if expired else timedelta(hours=1))
        connector = Connector(
            user_id=user.id,
            provider=provider,
            access_token_encrypted=encrypt_token("fake-access-token"),
            refresh_token_encrypted=encrypt_token("fake-refresh-token"),
            scopes="",
            token_expires_at=expires_at,
        )
        db.add(connector)
        await db.commit()
        await db.refresh(connector)
        return connector


def _fake_embed_texts(
    vector_size: int = EMBEDDING_DIM,
) -> tuple[list[list[str]], Callable[[list[str]], Awaitable[list[list[float]]]]]:
    """Returns (call log, fake embedder) — call log records each call's input texts."""
    calls: list[list[str]] = []

    async def fake_embed(texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        return [[float(len(text))] * vector_size for text in texts]

    return calls, fake_embed


async def test_sync_connector_inserts_and_dedupes_on_rerun(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = await _create_connector("gmail")
    items = [
        NormalizedItem(external_id="msg-1", content="Hello", item_metadata={"subject": "Hi"}),
        NormalizedItem(external_id="msg-2", content="World", item_metadata={"subject": "Yo"}),
    ]

    async def fake_fetch(_access_token: str, _sync_state: dict) -> SyncBatch:
        return SyncBatch(items, {})

    calls, fake_embed = _fake_embed_texts()
    monkeypatch.setitem(sync_module._PROVIDER_FETCHERS, "gmail", fake_fetch)
    monkeypatch.setattr(sync_module, "embed_texts", fake_embed)

    async with async_session_factory() as db:
        db_connector = await db.get(Connector, connector.id)
        assert db_connector is not None
        count = await sync_module.sync_connector(db_connector, db)
    assert count == 2
    assert calls == [["Hello", "World"]]  # both new items embedded once

    async with async_session_factory() as db:
        rows = (
            (await db.execute(select(MemoryItem).where(MemoryItem.user_id == connector.user_id)))
            .scalars()
            .all()
        )
    assert {row.external_id for row in rows} == {"msg-1", "msg-2"}
    assert all(row.embedding is not None for row in rows)

    # Re-running the sync with the same, unchanged items must not create
    # duplicate rows, and must not waste embedding calls on unchanged content.
    async with async_session_factory() as db:
        db_connector = await db.get(Connector, connector.id)
        assert db_connector is not None
        await sync_module.sync_connector(db_connector, db)

    assert calls == [["Hello", "World"]]  # no second call — nothing changed

    async with async_session_factory() as db:
        rows_after = (
            (await db.execute(select(MemoryItem).where(MemoryItem.user_id == connector.user_id)))
            .scalars()
            .all()
        )
    assert len(rows_after) == 2


async def test_sync_connector_reembeds_only_changed_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = await _create_connector("gmail")
    original_items = [NormalizedItem(external_id="msg-1", content="Hello", item_metadata={})]
    calls, fake_embed = _fake_embed_texts()
    monkeypatch.setattr(sync_module, "embed_texts", fake_embed)

    async def fetch_original(_access_token: str, _sync_state: dict) -> SyncBatch:
        return SyncBatch(original_items, {})

    monkeypatch.setitem(sync_module._PROVIDER_FETCHERS, "gmail", fetch_original)
    async with async_session_factory() as db:
        db_connector = await db.get(Connector, connector.id)
        assert db_connector is not None
        await sync_module.sync_connector(db_connector, db)

    updated_items = [NormalizedItem(external_id="msg-1", content="Hello, edited", item_metadata={})]

    async def fetch_updated(_access_token: str, _sync_state: dict) -> SyncBatch:
        return SyncBatch(updated_items, {})

    monkeypatch.setitem(sync_module._PROVIDER_FETCHERS, "gmail", fetch_updated)
    async with async_session_factory() as db:
        db_connector = await db.get(Connector, connector.id)
        assert db_connector is not None
        await sync_module.sync_connector(db_connector, db)

    assert calls == [["Hello"], ["Hello, edited"]]  # changed content re-embedded

    async with async_session_factory() as db:
        row = (
            await db.execute(
                select(MemoryItem).where(
                    MemoryItem.user_id == connector.user_id, MemoryItem.external_id == "msg-1"
                )
            )
        ).scalar_one()
    assert row.content == "Hello, edited"


async def test_sync_connector_refreshes_expired_token(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = await _create_connector("gmail", expired=True)
    refreshed = GoogleTokenResponse(
        access_token="new-access-token",
        refresh_token=None,
        scope="",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    captured_tokens: list[str] = []

    async def fake_refresh(_refresh_token: str) -> GoogleTokenResponse:
        return refreshed

    async def fake_fetch(access_token: str, _sync_state: dict) -> SyncBatch:
        captured_tokens.append(access_token)
        return SyncBatch([], {})

    monkeypatch.setattr(tokens_module.google_oauth, "refresh_access_token", fake_refresh)
    monkeypatch.setitem(sync_module._PROVIDER_FETCHERS, "gmail", fake_fetch)

    async with async_session_factory() as db:
        db_connector = await db.get(Connector, connector.id)
        assert db_connector is not None
        await sync_module.sync_connector(db_connector, db)

    assert captured_tokens == ["new-access-token"]

    async with async_session_factory() as db:
        updated = await db.get(Connector, connector.id)
        assert updated is not None
        assert updated.token_expires_at is not None
        assert updated.token_expires_at > datetime.now(UTC)
        assert decrypt_token(updated.access_token_encrypted) == "new-access-token"


async def test_poll_connectors_syncs_every_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    gmail_connector = await _create_connector("gmail")
    calendar_connector = await _create_connector("google_calendar")

    async def fake_sync_connector(connector: Connector, _db: object) -> int:
        return 1 if connector.id == gmail_connector.id else 2

    monkeypatch.setattr(connector_sync_task, "sync_connector", fake_sync_connector)

    results = await connector_sync_task._poll_connectors_async()

    assert results[f"{gmail_connector.user_id}:gmail"] == 1
    assert results[f"{calendar_connector.user_id}:google_calendar"] == 2


async def test_poll_connectors_one_failure_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken_connector = await _create_connector("gmail")
    healthy_connector = await _create_connector("google_calendar")

    async def flaky_sync_connector(connector: Connector, _db: object) -> int:
        if connector.id == broken_connector.id:
            raise RuntimeError("simulated decrypt failure")
        return 5

    monkeypatch.setattr(connector_sync_task, "sync_connector", flaky_sync_connector)

    results = await connector_sync_task._poll_connectors_async()

    assert results[f"{broken_connector.user_id}:gmail"] == "error"
    assert results[f"{healthy_connector.user_id}:google_calendar"] == 5


async def test_sync_stores_occurred_at_and_keeps_it_when_a_later_sync_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = await _create_connector("gmail")
    sent_at = datetime(2026, 9, 1, 8, 30, tzinfo=UTC)
    batches = [
        [NormalizedItem("msg-1", "Hello", {}, occurred_at=sent_at)],
        [NormalizedItem("msg-1", "Hello", {}, occurred_at=None)],
    ]

    async def fake_fetch(_access_token: str, _sync_state: dict) -> SyncBatch:
        return SyncBatch(batches.pop(0), {})

    _, fake_embed = _fake_embed_texts()
    monkeypatch.setitem(sync_module._PROVIDER_FETCHERS, "gmail", fake_fetch)
    monkeypatch.setattr(sync_module, "embed_texts", fake_embed)

    for _ in range(2):
        async with async_session_factory() as db:
            db_connector = await db.get(Connector, connector.id)
            assert db_connector is not None
            await sync_module.sync_connector(db_connector, db)

        async with async_session_factory() as db:
            item = (
                await db.execute(select(MemoryItem).where(MemoryItem.user_id == connector.user_id))
            ).scalar_one()
        assert item.occurred_at == sent_at


def _google_error(status_code: int, error: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://oauth2.googleapis.com/token")
    response = httpx.Response(status_code, json={"error": error}, request=request)
    return httpx.HTTPStatusError("token refresh failed", request=request, response=response)


async def test_dead_refresh_token_raises_connector_access_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = await _create_connector("gmail", expired=True)

    async def revoked(_refresh_token: str) -> GoogleTokenResponse:
        raise _google_error(400, "invalid_grant")

    monkeypatch.setattr(tokens_module.google_oauth, "refresh_access_token", revoked)
    async with async_session_factory() as db:
        db_connector = await db.get(Connector, connector.id)
        assert db_connector is not None
        with pytest.raises(tokens_module.ConnectorAccessExpired, match="reconnect Gmail"):
            await tokens_module.get_valid_access_token(db_connector, db)

    async def google_down(_refresh_token: str) -> GoogleTokenResponse:
        raise _google_error(503, "backend_error")

    # A transient Google failure is not a dead grant — it stays a normal, retryable error.
    monkeypatch.setattr(tokens_module.google_oauth, "refresh_access_token", google_down)
    async with async_session_factory() as db:
        db_connector = await db.get(Connector, connector.id)
        assert db_connector is not None
        with pytest.raises(httpx.HTTPStatusError):
            await tokens_module.get_valid_access_token(db_connector, db)


async def test_poll_connectors_reports_expired_access_as_needs_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = await _create_connector("gmail")

    async def expired_sync(conn: Connector, _db: object) -> int:
        if conn.id == connector.id:
            raise tokens_module.ConnectorAccessExpired("gmail")
        return 0

    monkeypatch.setattr(connector_sync_task, "sync_connector", expired_sync)
    results = await connector_sync_task._poll_connectors_async()

    assert results[f"{connector.user_id}:gmail"] == "needs_reconnect"
