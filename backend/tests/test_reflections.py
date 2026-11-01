import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

import app.services.reflections as refl_module
from app.db.session import async_session_factory
from app.main import app
from app.models.memory_item import MemoryItem
from app.models.reflection import Reflection
from app.services.reflections import (
    _Item,
    _split,
    compute_facts,
    parse_reflections,
    trim_speculation,
)
from app.workers.tasks.reflections import _generate_due_async

PASSWORD = "correct-horse-battery"


@asynccontextmanager
async def _signed_up_client() -> AsyncIterator[tuple[AsyncClient, uuid.UUID]]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        signup = await client.post(
            "/api/v1/auth/signup",
            json={"email": f"{uuid.uuid4()}@example.com", "password": PASSWORD},
        )
        yield client, uuid.UUID(signup.json()["id"])


async def _seed(user_id: uuid.UUID, n: int = 10) -> None:
    now = datetime.now(UTC)
    async with async_session_factory() as db:
        for i in range(n):
            db.add(
                MemoryItem(
                    user_id=user_id,
                    source="gmail" if i % 2 else "google_calendar",
                    external_id=f"m{i}",
                    content=f"Item {i}\n\nSecret body {i}",
                    item_metadata={"from": "Priya Nandan <priya@acme.com>"} if i % 2 else {},
                    occurred_at=now - timedelta(days=i + 1),
                )
            )
        await db.commit()


def _reply(*reflections: dict) -> str:
    return "Here you go:\n" + json.dumps({"reflections": list(reflections)})


GOOD = {
    "category": "relationships",
    "tone": "positive",
    "headline": "Priya keeps showing up.",
    "observation": "Priya has emailed you several times this month.",
    "tags": ["Work", "Priya"],
    "evidence": [1, 3],
}


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Patches the LLM call; returns a log of (prompt, memories) per call. Reply is GOOD."""
    calls: list[dict] = []

    async def fake(prompt: str, memories: str) -> str:
        calls.append({"prompt": prompt, "memories": memories})
        return _reply(GOOD)

    monkeypatch.setattr(refl_module, "_llm", fake)
    return calls


# --- unit ------------------------------------------------------------------------------------


def test_parse_reflections_validates_and_drops_uncited() -> None:
    parsed = parse_reflections(
        _reply(
            GOOD,
            {**GOOD, "evidence": []},  # uncited → dropped
            {**GOOD, "evidence": [99]},  # out of range → dropped
            {**GOOD, "observation": ""},  # empty → dropped
            {**GOOD, "category": "astrology", "tone": "ecstatic", "evidence": ["2", 2, "x"]},
        ),
        item_count=5,
    )
    assert len(parsed) == 2
    assert parsed[0]["headline"] == "Priya keeps showing up"  # trailing period stripped
    assert parsed[0]["evidence"] == [1, 3]
    assert (parsed[1]["category"], parsed[1]["tone"], parsed[1]["evidence"]) == (
        "personal",
        "neutral",
        [2],
    )
    assert parse_reflections("no json here", 5) == []
    assert parse_reflections('{"reflections": "nope"}', 5) == []


def test_trim_speculation_keeps_observations_descriptive() -> None:
    assert (
        trim_speculation(
            "You received five HDFC emails this week, indicating a focus on investing."
        )
        == "You received five HDFC emails this week."
    )
    assert (
        trim_speculation("Six meetings started before 9am — suggesting you prefer mornings.")
        == "Six meetings started before 9am."
    )
    plain = "Priya emailed you three times."
    assert trim_speculation(plain) == plain
    # Never trims an observation down to nothing.
    assert trim_speculation("Indicating something.") == "Indicating something."


def test_split_strips_invisible_padding() -> None:
    title, body = _split("Fund update\n\nStrong fundamentals \u200c \u200c \u200c &amp; more")
    assert (title, body) == ("Fund update", "Strong fundamentals & more")


def test_compute_facts_counts_in_local_time() -> None:
    def item(source: str, local: datetime, sender: str | None = None) -> _Item:
        memory = MemoryItem(source=source, external_id="x", content="t", item_metadata={})
        return _Item(memory, "t", "", local, sender)

    monday_early = datetime(2026, 10, 5, 8, 0)
    items = [
        item("google_calendar", monday_early),
        item("google_calendar", monday_early + timedelta(hours=11)),  # 19:00
        item("gmail", monday_early - timedelta(days=7), "Priya"),
        item("gmail", monday_early - timedelta(days=7), "Priya"),
    ]
    facts = "\n".join(compute_facts(items, upcoming=[], prior_fortnight=3))
    assert "Mon 2" in facts
    assert "1 started before 9:00 and 1 at or after 18:00" in facts
    assert "0 events in the next 14 days vs 3 in the previous 14 days" in facts
    assert "Priya (2)" in facts
    assert "week of Sep 28: 2" in facts and "week of Oct 05: 2" in facts


# --- API -------------------------------------------------------------------------------------


async def test_reflection_routes_require_auth() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/v1/reflections/")).status_code == 401
        assert (await client.post("/api/v1/reflections/generate", json={})).status_code == 401


async def test_generate_needs_enough_memories(fake_llm: list[dict]) -> None:
    async with _signed_up_client() as (client, user_id):
        await _seed(user_id, n=3)
        listing = (await client.get("/api/v1/reflections/")).json()
        assert listing["reflections"] == [] and listing["memory_count"] == 3
        assert listing["last_generated_at"] is None and listing["can_generate_at"] is None
        response = await client.post("/api/v1/reflections/generate", json={})
    assert response.status_code == 400
    assert fake_llm == []


async def test_generate_stores_grounded_reflections_and_rate_limits(fake_llm: list[dict]) -> None:
    async with _signed_up_client() as (client, user_id):
        await _seed(user_id)
        response = await client.post(
            "/api/v1/reflections/generate", json={"timezone": "Asia/Kolkata"}
        )
        assert response.status_code == 200
        body = response.json()
        again = await client.post("/api/v1/reflections/generate", json={})
        me = (await client.get("/api/v1/auth/me")).json()

    assert again.status_code == 429
    [reflection] = body["reflections"]
    assert reflection["headline"] == "Priya keeps showing up"
    assert reflection["tone"] == "positive" and reflection["tags"] == ["Work", "Priya"]
    # Evidence [1, 3] → the 1st and 3rd newest memories, snapshotted.
    assert [e["title"] for e in reflection["evidence"]] == ["Item 0", "Item 2"]
    assert body["last_generated_at"] and body["next_scheduled_at"] and body["can_generate_at"]
    assert me["timezone"] == "Asia/Kolkata"
    # Content access on (default) → snippets reach the model.
    assert "Secret body 1" in fake_llm[0]["memories"]
    assert "FACTS:" in fake_llm[0]["memories"]


async def test_content_off_keeps_bodies_away_from_the_model(fake_llm: list[dict]) -> None:
    async with _signed_up_client() as (client, user_id):
        await _seed(user_id)
        await client.patch("/api/v1/account/preferences", json={"allow_content_in_chat": False})
        await client.post("/api/v1/reflections/generate", json={})
    assert "Item 1" in fake_llm[0]["memories"]
    assert "Secret body" not in fake_llm[0]["memories"]


async def test_unusable_model_reply_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    async def junk(_prompt: str, _memories: str) -> str:
        return "I can't do that."

    monkeypatch.setattr(refl_module, "_llm", junk)
    async with _signed_up_client() as (client, user_id):
        await _seed(user_id)
        response = await client.post("/api/v1/reflections/generate", json={})
        listing = (await client.get("/api/v1/reflections/")).json()
    assert response.status_code == 503
    assert listing["reflections"] == [] and listing["last_generated_at"] is None


async def test_dismiss_restore_and_discuss(fake_llm: list[dict]) -> None:
    async with _signed_up_client() as (client, user_id):
        await _seed(user_id)
        [reflection] = (await client.post("/api/v1/reflections/generate", json={})).json()[
            "reflections"
        ]
        rid = reflection["id"]

        assert (await client.post(f"/api/v1/reflections/{rid}/dismiss")).status_code == 200
        assert (await client.get("/api/v1/reflections/")).json()["reflections"] == []
        assert (await client.post(f"/api/v1/reflections/{rid}/restore")).status_code == 200
        assert len((await client.get("/api/v1/reflections/")).json()["reflections"]) == 1

        first = (await client.post(f"/api/v1/reflections/{rid}/discuss")).json()
        second = (await client.post(f"/api/v1/reflections/{rid}/discuss")).json()
        assert first["conversation_id"] == second["conversation_id"]
        thread = (await client.get(f"/api/v1/chat/conversations/{first['conversation_id']}")).json()

    assert thread["title"] == "Priya keeps showing up"
    [message] = thread["messages"]
    assert message["role"] == "assistant"
    assert "Priya has emailed you several times" in message["content"]
    assert [c["title"] for c in message["citations"]] == ["Item 0", "Item 2"]
    assert [c["snippet"] for c in message["citations"]] == ["Secret body 0", "Secret body 2"]


async def test_other_users_reflection_is_not_found(fake_llm: list[dict]) -> None:
    async with _signed_up_client() as (owner, owner_id):
        await _seed(owner_id)
        rid = (await owner.post("/api/v1/reflections/generate", json={})).json()["reflections"][0][
            "id"
        ]
    async with _signed_up_client() as (intruder, _):
        for action in ("dismiss", "restore", "discuss"):
            response = await intruder.post(f"/api/v1/reflections/{rid}/{action}")
            assert response.status_code == 404
        assert (await intruder.get("/api/v1/reflections/")).json()["reflections"] == []


async def test_weekly_job_generates_only_for_due_users(fake_llm: list[dict]) -> None:
    async with _signed_up_client() as (_, due_id):
        await _seed(due_id)
    async with _signed_up_client() as (recent_client, recent_id):
        await _seed(recent_id)
        await recent_client.post("/api/v1/reflections/generate", json={})
    async with _signed_up_client() as (_, sparse_id):
        await _seed(sparse_id, n=2)

    # Scoped to this test's users — the sweep must never touch other accounts in the DB.
    results = await _generate_due_async(only={due_id, recent_id, sparse_id})

    assert results.get(str(due_id)) == 1
    assert str(recent_id) not in results
    assert str(sparse_id) not in results


async def test_export_includes_and_reset_deletes_reflections(fake_llm: list[dict]) -> None:
    async with _signed_up_client() as (client, user_id):
        await _seed(user_id)
        await client.post("/api/v1/reflections/generate", json={})
        export = json.loads((await client.get("/api/v1/account/export")).content)
        assert export["reflections"][0]["headline"] == "Priya keeps showing up"

        async def no_revoke(_token: str) -> bool:
            return True

        monkey = pytest.MonkeyPatch()
        monkey.setattr("app.services.oauth.google.revoke_token", no_revoke)
        try:
            reset = await client.post("/api/v1/account/reset", json={"password": PASSWORD})
        finally:
            monkey.undo()
    assert reset.status_code == 204
    async with async_session_factory() as db:
        remaining = await db.scalar(
            select(func.count()).select_from(Reflection).where(Reflection.user_id == user_id)
        )
    assert remaining == 0
