from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from app.core.config import get_settings

settings = get_settings()

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v2/userinfo"
_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

# One Google OAuth app can serve several of our "connectors" — each requests
# only the scopes it needs so a user can grant Gmail without also granting
# Calendar. Both include userinfo.email so we can label the connected account.
PROVIDER_SCOPES: dict[str, list[str]] = {
    "gmail": [
        "https://www.googleapis.com/auth/gmail.readonly",
        # Lets the twin send an email, but only ever behind the draft/confirm
        # flow in Phase 5 — nothing calls the send API without a user confirm.
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/userinfo.email",
    ],
    "google_calendar": [
        "https://www.googleapis.com/auth/calendar.readonly",
        # Lets the twin add events, but only behind the same draft/confirm flow as
        # gmail.send. Connectors granted before this was added must reconnect once.
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/userinfo.email",
    ],
}


@dataclass
class GoogleTokenResponse:
    access_token: str
    refresh_token: str | None
    scope: str | None
    expires_at: datetime | None


def build_authorize_url(provider: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(PROVIDER_SCOPES[provider]),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{_AUTH_ENDPOINT}?{urlencode(params)}"


async def _request_token(data: dict[str, str | None]) -> GoogleTokenResponse:
    async with httpx.AsyncClient() as client:
        response = await client.post(_TOKEN_ENDPOINT, data=data)
        response.raise_for_status()
        payload = response.json()

    expires_in = payload.get("expires_in")
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in) if expires_in else None
    return GoogleTokenResponse(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        scope=payload.get("scope"),
        expires_at=expires_at,
    )


async def exchange_code(code: str, redirect_uri: str) -> GoogleTokenResponse:
    return await _request_token(
        {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    )


async def refresh_access_token(refresh_token: str) -> GoogleTokenResponse:
    # Google does not re-send a refresh_token on refresh — callers must keep
    # using the original one (it stays valid until the user revokes access).
    return await _request_token(
        {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    )


async def fetch_userinfo(access_token: str) -> dict[str, str]:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            _USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"}
        )
        response.raise_for_status()
        return dict(response.json())


async def revoke_token(token: str) -> bool:
    """Revokes a Google access/refresh token (revoking a refresh token ends the whole grant).
    Best effort: returns False instead of raising, since callers are deleting data anyway.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(_REVOKE_ENDPOINT, data={"token": token})
        return response.status_code == 200
    except httpx.HTTPError:
        return False
