import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient

from app.db.session import async_session_factory
from app.main import app
from app.models.memory_item import MemoryItem
from app.services.ingestion.base import parse_email_date, parse_event_start
from app.services.ingestion.gmail import normalize_message
from app.services.ingestion.google_calendar import normalize_event


@asynccontextmanager
async def _signed_up_client() -> AsyncIterator[tuple[AsyncClient, uuid.UUID]]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        signup = await client.post(
            "/api/v1/auth/signup",
            json={"email": f"{uuid.uuid4()}@example.com", "password": "correct-horse-battery"},
        )
        yield client, uuid.UUID(signup.json()["id"])


async def _add(user_id: uuid.UUID, *items: dict) -> None:
    async with async_session_factory() as db:
        db.add_all(
            MemoryItem(user_id=user_id, item_metadata=item.pop("item_metadata", {}), **item)
            for item in items
        )
        await db.commit()


async def test_timeline_requires_auth() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/memory/timeline")
    assert response.status_code == 401


async def test_timeline_orders_by_occurred_at_and_splits_out_upcoming() -> None:
    now = datetime.now(UTC)
    async with _signed_up_client() as (client, user_id):
        await _add(
            user_id,
            # Indexed just now, but the email itself is from 3 days ago.
            {
                "source": "gmail",
                "external_id": "old-email",
                "content": "Invoice &amp; receipt\n\nIt&#39;s attached.",
                "item_metadata": {"from": "Priya <priya@example.com>"},
                "occurred_at": now - timedelta(days=3),
            },
            {
                "source": "gmail",
                "external_id": "new-email",
                "content": "Standup notes\n\nShipped the fix.",
                "occurred_at": now - timedelta(hours=1),
            },
            {
                "source": "google_calendar",
                "external_id": "far-event",
                "content": "Offsite",
                "item_metadata": {"all_day": True, "attendees": ["a@x.com", "b@x.com"]},
                "occurred_at": now + timedelta(days=9),
            },
            {
                "source": "google_calendar",
                "external_id": "soon-event",
                "content": "Design review\n\nQ3 roadmap",
                "occurred_at": now + timedelta(days=1),
            },
        )
        response = await client.get("/api/v1/memory/timeline")

    assert response.status_code == 200
    body = response.json()
    assert [i["title"] for i in body["items"]] == ["Standup notes", "Invoice & receipt"]
    old_email = body["items"][1]
    assert old_email["body"] == "It's attached."
    assert old_email["details"] == {"from": "Priya <priya@example.com>"}

    assert [i["title"] for i in body["upcoming"]] == ["Design review", "Offsite"]
    offsite = body["upcoming"][1]
    assert offsite["all_day"] is True
    assert offsite["body"] == ""
    assert offsite["details"]["attendee_count"] == 2

    assert {s["source"]: s["count"] for s in body["sources"]} == {
        "gmail": 2,
        "google_calendar": 2,
    }
    assert body["next_cursor"] is None


async def test_timeline_paginates_without_gaps_or_duplicates() -> None:
    now = datetime.now(UTC)
    async with _signed_up_client() as (client, user_id):
        # Two items share a timestamp to exercise the id tiebreaker.
        stamps = [now - timedelta(hours=h) for h in (1, 2, 2, 3, 4)]
        await _add(
            user_id,
            *(
                {
                    "source": "gmail",
                    "external_id": f"m{i}",
                    "content": f"m{i}",
                    "occurred_at": at,
                }
                for i, at in enumerate(stamps)
            ),
        )

        seen: list[str] = []
        cursor = None
        pages = 0
        while True:
            params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
            body = (await client.get("/api/v1/memory/timeline", params=params)).json()
            pages += 1
            seen += [i["title"] for i in body["items"]]
            if pages > 1:
                assert body["upcoming"] == [] and body["sources"] == []
            cursor = body["next_cursor"]
            if cursor is None:
                break

    assert pages == 3
    assert sorted(seen) == [f"m{i}" for i in range(5)]
    assert seen[0] == "m0" and seen[-1] == "m4"


async def test_timeline_filters_by_source_and_query() -> None:
    now = datetime.now(UTC)
    async with _signed_up_client() as (client, user_id):
        await _add(
            user_id,
            {
                "source": "gmail",
                "external_id": "a",
                "content": "Budget review\n\n100% approved",
                "occurred_at": now - timedelta(hours=1),
            },
            {
                "source": "gmail",
                "external_id": "b",
                "content": "Lunch plans",
                "occurred_at": now - timedelta(hours=2),
            },
            {
                "source": "google_calendar",
                "external_id": "c",
                "content": "Budget sync",
                "occurred_at": now - timedelta(hours=3),
            },
        )
        by_source = await client.get("/api/v1/memory/timeline", params={"source": "gmail"})
        by_query = await client.get("/api/v1/memory/timeline", params={"q": "BUDGET"})
        literal_percent = await client.get("/api/v1/memory/timeline", params={"q": "100%"})
        wildcard_only = await client.get("/api/v1/memory/timeline", params={"q": "%"})

    assert [i["title"] for i in by_source.json()["items"]] == ["Budget review", "Lunch plans"]
    assert [i["title"] for i in by_query.json()["items"]] == ["Budget review", "Budget sync"]
    assert [i["title"] for i in literal_percent.json()["items"]] == ["Budget review"]
    # "%" is matched literally, not as a wildcard that returns everything.
    assert [i["title"] for i in wildcard_only.json()["items"]] == ["Budget review"]


async def test_timeline_excludes_other_users_and_rejects_bad_cursor() -> None:
    async with _signed_up_client() as (_, other_id):
        await _add(other_id, {"source": "gmail", "external_id": "x", "content": "secret"})
    async with _signed_up_client() as (client, _):
        mine = await client.get("/api/v1/memory/timeline")
        bad = await client.get("/api/v1/memory/timeline", params={"cursor": "not-a-cursor"})

    assert mine.json()["items"] == []
    assert mine.json()["sources"] == []
    assert bad.status_code == 400


def test_gmail_prefers_internal_date_and_falls_back_to_header() -> None:
    base = {
        "id": "m1",
        "snippet": "hi",
        "payload": {"headers": [{"name": "Subject", "value": "S"}]},
    }
    with_internal = normalize_message({**base, "internalDate": "1790000000000"})
    assert with_internal.occurred_at == datetime.fromtimestamp(1790000000, tz=UTC)

    header_only = normalize_message(
        {
            **base,
            "payload": {"headers": [{"name": "Date", "value": "Thu, 24 Sep 2026 09:37:26 -0700"}]},
        }
    )
    assert header_only.occurred_at == datetime(2026, 9, 24, 16, 37, 26, tzinfo=UTC)
    assert parse_email_date("not a date") is None
    assert parse_email_date(None) is None


def test_calendar_start_parsing_handles_timed_and_all_day_events() -> None:
    timed = normalize_event(
        {"id": "e1", "summary": "Standup", "start": {"dateTime": "2026-09-25T10:00:00+05:30"}}
    )
    assert timed.occurred_at == datetime(2026, 9, 25, 4, 30, tzinfo=UTC)
    assert timed.item_metadata["all_day"] is False

    all_day = normalize_event({"id": "e2", "summary": "Trip", "start": {"date": "2026-10-02"}})
    assert all_day.occurred_at == datetime(2026, 10, 2, tzinfo=UTC)
    assert all_day.item_metadata["all_day"] is True

    assert parse_event_start("2026-09-25T10:00:00Z")[0] == datetime(2026, 9, 25, 10, tzinfo=UTC)
    assert parse_event_start("garbage") == (None, False)
