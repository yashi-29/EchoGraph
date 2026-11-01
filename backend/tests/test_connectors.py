import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from app.core.security import create_oauth_state_token
from app.main import app
from app.services.oauth.google import PROVIDER_SCOPES, GoogleTokenResponse


@asynccontextmanager
async def _signed_up_client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        email = f"{uuid.uuid4()}@example.com"
        await client.post(
            "/api/v1/auth/signup", json={"email": email, "password": "correct-horse-battery"}
        )
        yield client


async def test_connectors_require_auth() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        list_response = await client.get("/api/v1/connectors/")
        authorize_response = await client.get("/api/v1/connectors/gmail/authorize")

    assert list_response.status_code == 401
    assert authorize_response.status_code == 401


async def test_unknown_provider_is_404() -> None:
    async with _signed_up_client() as client:
        response = await client.get("/api/v1/connectors/carrier-pigeon/authorize")

    assert response.status_code == 404


async def test_authorize_redirects_to_google_with_state() -> None:
    async with _signed_up_client() as client:
        response = await client.get("/api/v1/connectors/gmail/authorize", follow_redirects=False)

    assert response.status_code in (302, 307)
    location = response.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "state=" in location
    assert "gmail.readonly" in location


async def test_callback_with_invalid_state_redirects_to_error() -> None:
    async with _signed_up_client() as client:
        response = await client.get(
            "/api/v1/connectors/gmail/callback",
            params={"code": "irrelevant", "state": "not-a-real-token"},
            follow_redirects=False,
        )

    assert response.status_code in (302, 307)
    assert "status=error" in response.headers["location"]


async def test_callback_success_stores_connector_and_disconnect_removes_it() -> None:
    state = create_oauth_state_token("gmail")
    fake_tokens = GoogleTokenResponse(
        access_token="fake-access-token",
        refresh_token="fake-refresh-token",
        scope=" ".join(PROVIDER_SCOPES["gmail"]),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    async with _signed_up_client() as client:
        with (
            patch(
                "app.api.v1.endpoints.connectors.google_oauth.exchange_code",
                new=AsyncMock(return_value=fake_tokens),
            ),
            patch(
                "app.api.v1.endpoints.connectors.google_oauth.fetch_userinfo",
                new=AsyncMock(return_value={"email": "twin-user@example.com"}),
            ),
        ):
            callback_response = await client.get(
                "/api/v1/connectors/gmail/callback",
                params={"code": "fake-code", "state": state},
                follow_redirects=False,
            )

        assert callback_response.status_code in (302, 307)
        assert "status=connected" in callback_response.headers["location"]

        listed = await client.get("/api/v1/connectors/")
        assert listed.status_code == 200
        connectors = listed.json()
        assert len(connectors) == 1
        assert connectors[0]["provider"] == "gmail"
        assert connectors[0]["external_account_email"] == "twin-user@example.com"

        revoke = AsyncMock(return_value=True)
        with patch("app.api.v1.endpoints.connectors.google_oauth.revoke_token", new=revoke):
            disconnect_response = await client.delete("/api/v1/connectors/gmail")
        assert disconnect_response.status_code == 204
        # The Google grant is withdrawn too, via the long-lived refresh token.
        revoke.assert_awaited_once_with("fake-refresh-token")

        listed_after = await client.get("/api/v1/connectors/")
        assert listed_after.json() == []


async def test_callback_with_unticked_permissions_is_refused() -> None:
    """Google's consent checkboxes start unticked; just clicking Continue grants only the
    email address. That must not be saved as a working connection."""
    state = create_oauth_state_token("gmail")
    email_only = GoogleTokenResponse(
        access_token="email-only-token",
        refresh_token="email-only-refresh",
        scope="openid https://www.googleapis.com/auth/userinfo.email",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    revoke = AsyncMock(return_value=True)

    async with _signed_up_client() as client:
        with (
            patch(
                "app.api.v1.endpoints.connectors.google_oauth.exchange_code",
                new=AsyncMock(return_value=email_only),
            ),
            patch(
                "app.api.v1.endpoints.connectors.google_oauth.fetch_userinfo",
                new=AsyncMock(return_value={"email": "twin-user@example.com"}),
            ),
            patch("app.api.v1.endpoints.connectors.google_oauth.revoke_token", new=revoke),
        ):
            response = await client.get(
                "/api/v1/connectors/gmail/callback",
                params={"code": "fake-code", "state": state},
                follow_redirects=False,
            )

        assert "status=missing_permissions" in response.headers["location"]
        revoke.assert_awaited_once_with("email-only-token")
        assert (await client.get("/api/v1/connectors/")).json() == []
