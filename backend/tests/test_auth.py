import uuid

from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app

settings = get_settings()


async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_signup_sets_session_cookie_and_returns_user() -> None:
    email = f"{uuid.uuid4()}@example.com"
    async with await _client() as client:
        response = await client.post(
            "/api/v1/auth/signup", json={"email": email, "password": "correct-horse-battery"}
        )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == email
    assert "password" not in body
    assert settings.session_cookie_name in response.cookies


async def test_signup_duplicate_email_is_rejected() -> None:
    email = f"{uuid.uuid4()}@example.com"
    async with await _client() as client:
        first = await client.post(
            "/api/v1/auth/signup", json={"email": email, "password": "correct-horse-battery"}
        )
        second = await client.post(
            "/api/v1/auth/signup", json={"email": email, "password": "another-password"}
        )

    assert first.status_code == 201
    assert second.status_code == 409


async def test_login_with_wrong_password_is_rejected() -> None:
    email = f"{uuid.uuid4()}@example.com"
    async with await _client() as client:
        await client.post(
            "/api/v1/auth/signup", json={"email": email, "password": "correct-horse-battery"}
        )
        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": "wrong-password"}
        )

    assert response.status_code == 401


async def test_me_requires_session_and_login_round_trip_works() -> None:
    email = f"{uuid.uuid4()}@example.com"
    async with await _client() as client:
        anonymous = await client.get("/api/v1/auth/me")
        assert anonymous.status_code == 401

        await client.post(
            "/api/v1/auth/signup", json={"email": email, "password": "correct-horse-battery"}
        )
        authed = await client.get("/api/v1/auth/me")
        assert authed.status_code == 200
        assert authed.json()["email"] == email

        await client.post("/api/v1/auth/logout")
        after_logout = await client.get("/api/v1/auth/me")
        assert after_logout.status_code == 401

        login_response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
        )
        assert login_response.status_code == 200
        authed_again = await client.get("/api/v1/auth/me")
        assert authed_again.status_code == 200
