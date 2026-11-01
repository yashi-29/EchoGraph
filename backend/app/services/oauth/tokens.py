from datetime import UTC, datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_token, encrypt_token
from app.models.connector import Connector
from app.services.oauth import google as google_oauth

_APP_NAMES = {"gmail": "Gmail", "google_calendar": "Google Calendar"}


class ConnectorAccessExpired(RuntimeError):
    """Google no longer honours this connector's refresh token (revoked by the user, or
    expired — apps in OAuth "Testing" mode get 7-day refresh tokens). Only reconnecting
    fixes it; the message is safe to show the user."""

    def __init__(self, provider: str) -> None:
        app = _APP_NAMES.get(provider, provider)
        super().__init__(
            f"{app} access has expired — reconnect {app} on the Connectors page, then try again."
        )
        self.provider = provider


async def get_valid_access_token(connector: Connector, db: AsyncSession) -> str:
    """Returns a usable access token for this connector, refreshing it first if expired."""
    if connector.token_expires_at is None or connector.token_expires_at > datetime.now(UTC):
        return decrypt_token(connector.access_token_encrypted)

    if not connector.refresh_token_encrypted:
        raise ConnectorAccessExpired(connector.provider)

    try:
        refreshed = await google_oauth.refresh_access_token(
            decrypt_token(connector.refresh_token_encrypted)
        )
    except httpx.HTTPStatusError as exc:
        # invalid_grant = the refresh token itself is dead; anything else (5xx, quota) is
        # transient and should surface as a normal error to retry later.
        if exc.response.status_code == 400 and _error_code(exc.response) == "invalid_grant":
            raise ConnectorAccessExpired(connector.provider) from exc
        raise
    connector.access_token_encrypted = encrypt_token(refreshed.access_token)
    connector.token_expires_at = refreshed.expires_at
    await db.commit()
    return refreshed.access_token


def _error_code(response: httpx.Response) -> str | None:
    try:
        return str(response.json().get("error"))
    except ValueError:
        return None
