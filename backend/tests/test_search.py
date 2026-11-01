import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

import app.services.search as search_module
from app.db.session import async_session_factory
from app.main import app
from app.models.graph_snapshot import GraphSnapshot
from app.models.memory_item import EMBEDDING_DIM, MemoryItem
from app.models.upload import Upload
from app.services.search import parse_terms, snippet


@pytest.fixture(autouse=True)
def _fresh_embedding_cache() -> Iterator[None]:
    search_module._embedding_cache.clear()
    yield
    search_module._embedding_cache.clear()


@asynccontextmanager
async def _signed_up_client() -> AsyncIterator[tuple[AsyncClient, uuid.UUID]]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        signup = await client.post(
            "/api/v1/auth/signup",
            json={"email": f"{uuid.uuid4()}@example.com", "password": "correct-horse-battery"},
        )
        yield client, uuid.UUID(signup.json()["id"])


def _vec(similarity_to_axis0: float) -> list[float]:
    """Unit vector whose cosine similarity with e0 is exactly `similarity_to_axis0`."""
    v = [0.0] * EMBEDDING_DIM
    v[0] = similarity_to_axis0
    v[1] = (1 - similarity_to_axis0**2) ** 0.5
    return v


def _axis0() -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[0] = 1.0
    return v


async def _add(user_id: uuid.UUID, *items: dict) -> None:
    now = datetime.now(UTC)
    async with async_session_factory() as db:
        for n, item in enumerate(items):
            db.add(
                MemoryItem(
                    user_id=user_id,
                    item_metadata=item.pop("item_metadata", {}),
                    occurred_at=item.pop("occurred_at", now - timedelta(hours=n)),
                    **item,
                )
            )
        await db.commit()


async def _no_embeddings(_text: str) -> list[float]:
    raise RuntimeError("no Voyage key in tests")


# --- unit ------------------------------------------------------------------------------------


def test_parse_terms() -> None:
    assert parse_terms('Vendor  "final pricing" vendor') == ["vendor", "final pricing"]
    assert parse_terms("a roadmap") == ["roadmap"]
    assert parse_terms("a") == ["a"]


def test_snippet_centres_on_first_match_and_unescapes() -> None:
    text = "intro " * 60 + "the vendor&#39;s final pricing is here " + "outro " * 60
    result = snippet(text, ["pricing"])
    assert "vendor's final pricing" in result
    assert result.startswith("…") and result.endswith("…")
    assert len(result) <= 245
    assert snippet("short text", ["zzz"]) == "short text"


# --- API -------------------------------------------------------------------------------------


async def test_search_requires_auth() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/v1/memory/search", params={"q": "x"})).status_code == 401
        assert (await client.get("/api/v1/memory/search/suggestions")).status_code == 401


async def test_keyword_search_requires_every_term_and_degrades_without_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(search_module, "embed_query", _no_embeddings)
    async with _signed_up_client() as (client, user_id):
        await _add(
            user_id,
            {
                "source": "gmail",
                "external_id": "m1",
                "content": "Vendor pricing &amp; terms\n\nFinal vendor pricing for the renewal.",
                "item_metadata": {"from": "Priya <priya@acme.com>"},
            },
            {
                "source": "gmail",
                "external_id": "m2",
                "content": "Vendor lunch\n\nNothing about money.",
            },
            {
                "source": "google_calendar",
                "external_id": "e1",
                "content": "Pricing review\n\nWith the vendor team",
                "item_metadata": {"html_link": "https://calendar.google.com/event?eid=1"},
            },
            {
                "source": "bookmark",
                "external_id": "b1",
                "content": "Vendor pricing guide\n\nhttps://example.com/guide",
                "item_metadata": {"url": "javascript:alert(1)"},
            },
        )
        body = (await client.get("/api/v1/memory/search", params={"q": "vendor pricing"})).json()

    assert body["semantic"] is False
    assert body["terms"] == ["vendor", "pricing"]
    by_id = {r["title"]: r for r in body["results"]}
    assert set(by_id) == {"Vendor pricing & terms", "Pricing review", "Vendor pricing guide"}
    assert all(r["match"] == "keyword" for r in body["results"])
    email = by_id["Vendor pricing & terms"]
    assert email["url"] == "https://mail.google.com/mail/u/0/#all/m1"
    assert email["details"]["from"] == "Priya <priya@acme.com>"
    assert by_id["Pricing review"]["url"] == "https://calendar.google.com/event?eid=1"
    # Only https links are passed through.
    assert by_id["Vendor pricing guide"]["url"] is None


async def test_semantic_results_are_cut_off_and_fused_with_keyword(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_embed(_text: str) -> list[float]:
        return _axis0()

    monkeypatch.setattr(search_module, "embed_query", fake_embed)
    async with _signed_up_client() as (client, user_id):
        await _add(
            user_id,
            # Keyword + semantic → ranked first.
            {"source": "gmail", "external_id": "both", "content": "Mutual fund statement",
             "embedding": _vec(0.55)},
            # Semantic only (no keyword), close to the best → included.
            {"source": "gmail", "external_id": "near", "content": "SIP investment update",
             "embedding": _vec(0.50)},
            # Semantic only, but far below the best/floor → excluded.
            {"source": "gmail", "external_id": "far", "content": "Hiking gear sale",
             "embedding": _vec(0.20)},
        )  # fmt: skip
        body = (await client.get("/api/v1/memory/search", params={"q": "mutual fund"})).json()

    assert body["semantic"] is True
    assert [(r["title"], r["match"]) for r in body["results"]] == [
        ("Mutual fund statement", "both"),
        ("SIP investment update", "semantic"),
    ]


async def test_gibberish_query_returns_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_embed(_text: str) -> list[float]:
        return _axis0()

    monkeypatch.setattr(search_module, "embed_query", fake_embed)
    async with _signed_up_client() as (client, user_id):
        await _add(
            user_id,
            {
                "source": "gmail",
                "external_id": "x",
                "content": "Newsletter",
                "embedding": _vec(0.3),
            },
        )
        body = (await client.get("/api/v1/memory/search", params={"q": "zzqqxx"})).json()
    assert body["results"] == [] and body["total"] == 0


async def test_query_embedding_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def fake_embed(_text: str) -> list[float]:
        nonlocal calls
        calls += 1
        return _axis0()

    monkeypatch.setattr(search_module, "embed_query", fake_embed)
    async with _signed_up_client() as (client, _):
        await client.get("/api/v1/memory/search", params={"q": "Roadmap"})
        await client.get("/api/v1/memory/search", params={"q": "  roadmap "})
    assert calls == 1


async def test_source_filter_sort_pagination_and_chunk_collapse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(search_module, "embed_query", _no_embeddings)
    now = datetime.now(UTC)
    async with _signed_up_client() as (client, user_id):
        async with async_session_factory() as db:
            upload = Upload(
                user_id=user_id, kind="document", filename="plan.pdf",
                content_type="application/pdf", size_bytes=1, sha256="0" * 64, data=b"x",
            )  # fmt: skip
            db.add(upload)
            await db.commit()
            upload_id = upload.id
        await _add(
            user_id,
            *(
                {
                    "source": "gmail",
                    "external_id": f"g{i}",
                    "content": f"Budget note {i}",
                    "occurred_at": now - timedelta(days=i),
                }
                for i in range(5)
            ),
            *(
                {
                    "source": "document",
                    "external_id": f"plan.pdf#{i}",
                    "content": f"Plan\n\nBudget section {i}",
                    "upload_id": upload_id,
                    "item_metadata": {"filename": "plan.pdf", "chunk_index": i},
                    "occurred_at": now - timedelta(days=10 + i),
                }
                for i in range(3)
            ),
        )
        page1 = (
            await client.get(
                "/api/v1/memory/search", params={"q": "budget", "limit": 3, "sort": "newest"}
            )
        ).json()
        page2 = (
            await client.get(
                "/api/v1/memory/search",
                params={"q": "budget", "limit": 3, "offset": 3, "sort": "newest"},
            )
        ).json()
        docs = (
            await client.get("/api/v1/memory/search", params={"q": "budget", "source": "document"})
        ).json()

    # 5 emails + the upload once (3 chunks collapsed) = 6.
    assert page1["total"] == 6 and page1["has_more"] is True and page2["has_more"] is False
    titles = [r["title"] for r in page1["results"] + page2["results"]]
    assert titles == [f"Budget note {i}" for i in range(5)] + ["Plan"]
    assert [r["source"] for r in docs["results"]] == ["document"]


async def test_search_only_sees_own_memories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_module, "embed_query", _no_embeddings)
    async with _signed_up_client() as (_, other_id):
        await _add(other_id, {"source": "gmail", "external_id": "s", "content": "secret plan"})
    async with _signed_up_client() as (client, _):
        body = (await client.get("/api/v1/memory/search", params={"q": "secret"})).json()
    assert body["results"] == []


async def test_suggestions_from_graph_snapshot_or_keywords() -> None:
    async with _signed_up_client() as (client, user_id):
        empty = (await client.get("/api/v1/memory/search/suggestions")).json()
        assert empty == {"suggestions": [], "sources": [], "total": 0}

        await _add(
            user_id,
            *(
                {"source": "gmail", "external_id": f"m{i}", "content": f"Invoice {i} overdue"}
                for i in range(3)
            ),
        )
        from_keywords = (await client.get("/api/v1/memory/search/suggestions")).json()
        assert from_keywords["total"] == 3
        assert from_keywords["sources"] == [{"source": "gmail", "count": 3}]
        assert "invoice" in from_keywords["suggestions"]

        async with async_session_factory() as db:
            db.add(
                GraphSnapshot(
                    user_id=user_id,
                    fingerprint="x",
                    data={
                        "nodes": [
                            {"id": "you", "type": "you", "label": "You"},
                            {"id": "topic:0", "type": "topic", "label": "Mutual Funds"},
                            {"id": "org:x", "type": "organization", "label": "HDFC MF"},
                        ]
                    },
                )
            )
            await db.commit()
        from_graph = (await client.get("/api/v1/memory/search/suggestions")).json()
    assert from_graph["suggestions"] == ["Mutual Funds", "HDFC MF"]


async def test_keyword_search_matches_sender_and_filename(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_module, "embed_query", _no_embeddings)
    async with _signed_up_client() as (client, user_id):
        await _add(
            user_id,
            {
                "source": "gmail",
                "external_id": "h1",
                "content": "NFO now open\n\nInvest today",
                "item_metadata": {"from": "HDFC MF <noresponse@hdfcfund.com>"},
            },
            {
                "source": "google_calendar",
                "external_id": "c1",
                "content": "Roadmap sync",
                "item_metadata": {"attendees": ["dana.lee@company.io"]},
            },
            {
                "source": "document",
                "external_id": "d1",
                "content": "Page one\n\nIntro text",
                "item_metadata": {"filename": "offer-letter.pdf"},
            },
        )
        by_sender = (await client.get("/api/v1/memory/search", params={"q": "hdfc nfo"})).json()
        by_file = (await client.get("/api/v1/memory/search", params={"q": "offer-letter"})).json()
        by_attendee = (await client.get("/api/v1/memory/search", params={"q": "dana"})).json()
    assert [r["title"] for r in by_sender["results"]] == ["NFO now open"]
    assert [r["title"] for r in by_file["results"]] == ["Page one"]
    assert [r["title"] for r in by_attendee["results"]] == ["Roadmap sync"]
