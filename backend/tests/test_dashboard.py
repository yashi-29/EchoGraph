import uuid
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient

from app.db.session import async_session_factory
from app.main import app
from app.models.connector import Connector
from app.models.memory_item import EMBEDDING_DIM, MemoryItem


async def test_dashboard_requires_auth() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/dashboard/")
    assert response.status_code == 401


async def test_dashboard_empty_account() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        email = f"{uuid.uuid4()}@example.com"
        await client.post(
            "/api/v1/auth/signup", json={"email": email, "password": "correct-horse-battery"}
        )
        response = await client.get("/api/v1/dashboard/")

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == email
    assert body["memories"] == {
        "total": 0,
        "today": 0,
        "this_week": 0,
        "last_week": 0,
        "searchable": 0,
    }
    assert body["sources"]["connected"] == 0
    assert body["last_indexed_at"] is None
    assert body["activity"] == []


async def test_dashboard_counts_only_current_users_data() -> None:
    now = datetime.now(UTC)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        signup = await client.post(
            "/api/v1/auth/signup",
            json={"email": f"{uuid.uuid4()}@example.com", "password": "correct-horse-battery"},
        )
        user_id = uuid.UUID(signup.json()["id"])
        other = await client.post(
            "/api/v1/auth/signup",
            json={"email": f"{uuid.uuid4()}@example.com", "password": "correct-horse-battery"},
        )
        other_id = uuid.UUID(other.json()["id"])

        async with async_session_factory() as db:
            db.add_all(
                [
                    MemoryItem(
                        user_id=user_id,
                        source="gmail",
                        external_id="today-1",
                        content="a",
                        item_metadata={},
                        embedding=[0.0] * EMBEDDING_DIM,
                        created_at=now,
                    ),
                    MemoryItem(
                        user_id=user_id,
                        source="gmail",
                        external_id="today-2",
                        content="b",
                        item_metadata={},
                        created_at=now,
                    ),
                    MemoryItem(
                        user_id=user_id,
                        source="google_calendar",
                        external_id="last-week",
                        content="c",
                        item_metadata={},
                        created_at=now - timedelta(days=10),
                    ),
                    MemoryItem(
                        user_id=other_id,
                        source="gmail",
                        external_id="someone-else",
                        content="d",
                        item_metadata={},
                        created_at=now,
                    ),
                    Connector(
                        user_id=user_id,
                        provider="gmail",
                        access_token_encrypted="x",
                        scopes="",
                    ),
                ]
            )
            await db.commit()

        # Log back in as the first user (signup of `other` replaced the session cookie).
        client.cookies.clear()
        me = signup.json()["email"]
        await client.post(
            "/api/v1/auth/login", json={"email": me, "password": "correct-horse-battery"}
        )
        response = await client.get("/api/v1/dashboard/")

    assert response.status_code == 200
    body = response.json()
    assert body["memories"] == {
        "total": 3,
        "today": 2,
        "this_week": 2,
        "last_week": 1,
        "searchable": 1,
    }
    assert body["sources"] == {"connected": 1, "available": 2}
    kinds = {(a["kind"], a["source"]) for a in body["activity"]}
    assert ("sync", "gmail") in kinds
    assert ("sync", "google_calendar") in kinds
    assert ("connected", "gmail") in kinds
    gmail_sync = next(a for a in body["activity"] if a["kind"] == "sync" and a["source"] == "gmail")
    assert gmail_sync["count"] == 2
