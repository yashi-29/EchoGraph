import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

import app.services.graph.build as build_module
import app.services.graph.topics as topics_module
from app.core.crypto import encrypt_token
from app.db.session import async_session_factory
from app.main import app
from app.models.connector import Connector
from app.models.memory_item import EMBEDDING_DIM, MemoryItem
from app.services.graph.entities import classify_address, registrable_domain
from app.services.graph.topics import cluster_count, cluster_keywords, kmeans, llm_labels

ME = "me@gmail.com"


@asynccontextmanager
async def _signed_up_client() -> AsyncIterator[tuple[AsyncClient, uuid.UUID]]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        signup = await client.post(
            "/api/v1/auth/signup",
            json={"email": f"{uuid.uuid4()}@example.com", "password": "correct-horse-battery"},
        )
        yield client, uuid.UUID(signup.json()["id"])


def _near(axis: int, jitter: float) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[axis] = 1.0
    vector[(axis + 7) % EMBEDDING_DIM] = jitter
    return vector


async def _seed(user_id: uuid.UUID) -> None:
    now = datetime.now(UTC)
    rows = [
        # Two emails from a real person, about invoices (cluster on axis 0).
        (
            "gmail",
            "p1",
            "Invoice for March\n\nPlease pay the invoice",
            '"Priya Nandan" <priya@acme.com>',
            0,
        ),
        ("gmail", "p2", "Invoice reminder\n\nInvoice overdue", "Priya Nandan <priya@acme.com>", 0),
        # Automated sender → organization, grouped by registrable domain (cluster on axis 0).
        (
            "gmail",
            "o1",
            "Your invoice receipt\n\nInvoice paid",
            "Acme Billing <no-reply@mail.acme-pay.io>",
            0,
        ),
        # The user's own sent mail → no node.
        ("gmail", "s1", "Invoice question\n\nSent by me", f"Me <{ME}>", 0),
    ]
    async with async_session_factory() as db:
        for n, (source, ext, content, sender, axis) in enumerate(rows):
            db.add(
                MemoryItem(
                    user_id=user_id,
                    source=source,
                    external_id=ext,
                    content=content,
                    item_metadata={"from": sender},
                    embedding=_near(axis, n * 0.01),
                    occurred_at=now - timedelta(days=n),
                )
            )
        # Calendar events on axis 1 with two attendees each → person↔person edge.
        for n in range(4):
            db.add(
                MemoryItem(
                    user_id=user_id,
                    source="google_calendar",
                    external_id=f"e{n}",
                    content=f"Hiking trip planning {n}\n\nTrail and hiking gear",
                    item_metadata={
                        "attendees": [
                            "sam.ruiz@example.org",
                            "dana@example.org",
                            ME,
                            "room-4b@resource.calendar.google.com",
                        ]
                    },
                    embedding=_near(1, n * 0.01),
                    occurred_at=now - timedelta(days=10 + n),
                )
            )
        db.add(
            Connector(
                user_id=user_id,
                provider="gmail",
                external_account_email=ME,
                access_token_encrypted=encrypt_token("x"),
                scopes="",
            )
        )
        await db.commit()


# --- unit: entities ---------------------------------------------------------------------------


def test_classify_address_people_organizations_and_skips() -> None:
    me = {ME}
    person = classify_address('"Priya Nandan" <Priya@Acme.com>', me)
    assert person is not None
    assert (person.kind, person.key, person.name) == (
        "person",
        "person:priya@acme.com",
        "Priya Nandan",
    )

    bare = classify_address("sam.ruiz@example.org", me)
    assert bare is not None and bare.kind == "person" and bare.name == "Sam Ruiz"

    noreply = classify_address("Google <noreply-accounts@google.com>", me)
    assert noreply is not None
    assert (noreply.kind, noreply.key, noreply.name) == ("organization", "org:google.com", "Google")

    bulk = classify_address("Kallaway <kallaway@mail.blog.sandcastles.ai>", me)
    assert bulk is not None and bulk.key == "org:sandcastles.ai"

    assert classify_address(f"Me <{ME}>", me) is None
    assert classify_address("room@resource.calendar.google.com", me) is None
    assert classify_address("not an address", me) is None


def test_registrable_domain() -> None:
    assert registrable_domain("mail.blog.sandcastles.ai") == "sandcastles.ai"
    assert registrable_domain("news.bbc.co.uk") == "bbc.co.uk"
    assert registrable_domain("google.com") == "google.com"


# --- unit: topics -----------------------------------------------------------------------------


def test_cluster_count_bounds() -> None:
    assert cluster_count(5) == 0
    assert cluster_count(6) == 2
    assert cluster_count(10_000) == topics_module.MAX_TOPICS


def test_kmeans_separates_clear_clusters_deterministically() -> None:
    rng = np.random.default_rng(1)
    a = np.eye(16)[0] + rng.normal(0, 0.05, (10, 16))
    b = np.eye(16)[5] + rng.normal(0, 0.05, (10, 16))
    vectors = np.vstack([a, b])
    labels = kmeans(vectors, 2)
    assert len(set(labels[:10])) == 1 and len(set(labels[10:])) == 1
    assert labels[0] != labels[10]
    assert np.array_equal(labels, kmeans(vectors, 2))


def test_cluster_keywords_prefers_distinctive_words_and_unescapes() -> None:
    keywords = cluster_keywords(
        [
            ["Invoice due &amp; overdue", "Invoice paid", "India&#39;s fund invoice"],
            ["Hiking trail", "Hiking gear", "Trail map"],
        ],
        ignore={"myname"},
    )
    assert keywords[0][0] == "invoice"
    assert set(keywords[1][:2]) == {"hiking", "trail"}
    assert not any("&" in w for ws in keywords for w in ws)


async def test_llm_labels_falls_back_on_bad_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(topics_module.settings, "groq_api_key", "test-key")

    class FakeGroq:
        def __init__(self, **_kwargs: object) -> None:
            async def create(**_kw: object) -> object:
                message = SimpleNamespace(content="Sure! Here are some labels: Invoices, Hiking")
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    monkeypatch.setattr(topics_module, "AsyncGroq", FakeGroq)
    labels = await llm_labels([["a"], ["b"]], [["invoice", "due"], ["hiking"]])
    assert labels == ["Invoice · Due", "Hiking"]


async def test_llm_labels_uses_model_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(topics_module.settings, "groq_api_key", "test-key")

    class FakeGroq:
        def __init__(self, **_kwargs: object) -> None:
            async def create(**_kw: object) -> object:
                message = SimpleNamespace(content='{"labels": ["Invoices", "Hiking Trips"]}')
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    monkeypatch.setattr(topics_module, "AsyncGroq", FakeGroq)
    assert await llm_labels([["a"], ["b"]], [["x"], ["y"]]) == ["Invoices", "Hiking Trips"]


# --- API ----------------------------------------------------------------------------------------


async def test_graph_requires_auth() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/v1/graph/")).status_code == 401


async def test_empty_account_graph_is_just_you() -> None:
    async with _signed_up_client() as (client, _):
        body = (await client.get("/api/v1/graph/")).json()
    assert [n["id"] for n in body["nodes"]] == ["you"]
    assert body["edges"] == []
    assert body["total_memories"] == 0


async def test_graph_nodes_edges_and_details(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_labels(titles: list[list[str]], keywords: list[list[str]]) -> list[str]:
        return ["Invoices" if any("nvoice" in t for t in ts) else "Hiking" for ts in titles]

    monkeypatch.setattr(build_module, "llm_labels", fake_labels)
    async with _signed_up_client() as (client, user_id):
        await _seed(user_id)
        body = (await client.get("/api/v1/graph/")).json()

    nodes = {n["id"]: n for n in body["nodes"]}
    edges = {(e["source"], e["target"]): e["weight"] for e in body["edges"]}

    assert nodes["you"]["count"] == 8
    assert nodes["you"]["sources"] == {"gmail": 4, "google_calendar": 4}

    priya = nodes["person:priya@acme.com"]
    assert (priya["type"], priya["label"], priya["count"]) == ("person", "Priya Nandan", 2)
    assert [r["title"] for r in priya["recent"]] == ["Invoice for March", "Invoice reminder"]
    org = nodes["org:acme-pay.io"]
    assert (org["type"], org["label"], org["subtitle"]) == (
        "organization",
        "Acme Billing",
        "acme-pay.io",
    )
    assert "person:sam.ruiz@example.org" in nodes and "person:dana@example.org" in nodes
    # The user themself and the meeting room never become nodes.
    assert not any(ME in key or "resource.calendar" in key for key in nodes)

    topics = {n["label"]: n for n in body["nodes"] if n["type"] == "topic"}
    assert set(topics) == {"Invoices", "Hiking"}
    assert topics["Invoices"]["count"] == 4 and topics["Hiking"]["count"] == 4

    assert edges[("you", "person:priya@acme.com")] == 2
    assert edges[("person:priya@acme.com", topics["Invoices"]["id"])] == 2
    assert edges[("person:sam.ruiz@example.org", topics["Hiking"]["id"])] == 4
    # Co-attendance links the two attendees directly.
    assert edges[("person:dana@example.org", "person:sam.ruiz@example.org")] == 4


async def test_graph_is_cached_until_memories_change(monkeypatch: pytest.MonkeyPatch) -> None:
    builds = 0
    real_build = build_module.build_graph

    async def counting_build(*args: object, **kwargs: object) -> object:
        nonlocal builds
        builds += 1
        return await real_build(*args, **kwargs)  # type: ignore[arg-type]

    async def fake_labels(titles: list[list[str]], keywords: list[list[str]]) -> list[str]:
        return [f"T{i}" for i in range(len(titles))]

    monkeypatch.setattr(build_module, "build_graph", counting_build)
    monkeypatch.setattr(build_module, "llm_labels", fake_labels)
    async with _signed_up_client() as (client, user_id):
        await _seed(user_id)
        first = (await client.get("/api/v1/graph/")).json()
        second = (await client.get("/api/v1/graph/")).json()
        assert builds == 1
        assert first == second

        async with async_session_factory() as db:
            db.add(
                MemoryItem(
                    user_id=user_id,
                    source="gmail",
                    external_id="new",
                    content="Fresh email",
                    item_metadata={"from": "New Person <new@person.dev>"},
                )
            )
            await db.commit()
        third = (await client.get("/api/v1/graph/")).json()

    assert builds == 2
    assert third["total_memories"] == 9
    assert "person:new@person.dev" in {n["id"] for n in third["nodes"]}


async def test_graph_only_includes_own_memories(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_labels(titles: list[list[str]], keywords: list[list[str]]) -> list[str]:
        return [f"T{i}" for i in range(len(titles))]

    monkeypatch.setattr(build_module, "llm_labels", fake_labels)
    async with _signed_up_client() as (_, other_id):
        await _seed(other_id)
    async with _signed_up_client() as (client, _):
        body = (await client.get("/api/v1/graph/")).json()
    assert [n["id"] for n in body["nodes"]] == ["you"]
