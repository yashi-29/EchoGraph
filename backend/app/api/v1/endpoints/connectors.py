import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.crypto import decrypt_token, encrypt_token
from app.core.security import create_oauth_state_token, verify_oauth_state_token
from app.db.session import get_db
from app.models.connector import Connector
from app.models.user import User
from app.schemas.connector import ConnectorRead
from app.services.oauth import google as google_oauth

router = APIRouter()
settings = get_settings()

# Phase 2 rollout order: Gmail, Google Calendar, Google Drive, Outlook
# Calendar, Notion, Slack. Only providers with a working OAuth implementation
# are listed here — WhatsApp/Apple Notes stay deferred (see ARCHITECTURE.md).
_GOOGLE_PROVIDERS = set(google_oauth.PROVIDER_SCOPES)
_SUPPORTED_PROVIDERS = _GOOGLE_PROVIDERS


def _require_supported_provider(provider: str) -> None:
    if provider not in _SUPPORTED_PROVIDERS:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Unknown or not-yet-supported connector: {provider}"
        )


def missing_permissions(provider: str, granted: set[str]) -> list[str]:
    """Requested scopes the user didn't tick on Google's consent screen."""
    return [scope for scope in google_oauth.PROVIDER_SCOPES[provider] if scope not in granted]


def _redirect_uri(provider: str) -> str:
    return f"{settings.backend_base_url}/api/v1/connectors/{provider}/callback"


@router.get("/", response_model=list[ConnectorRead])
async def list_connectors(
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[Connector]:
    result = await db.execute(select(Connector).where(Connector.user_id == current_user.id))
    return list(result.scalars().all())


@router.get("/{provider}/authorize")
async def authorize(
    provider: str, current_user: User = Depends(get_current_user)
) -> RedirectResponse:
    _require_supported_provider(provider)
    state = create_oauth_state_token(provider)
    url = google_oauth.build_authorize_url(provider, _redirect_uri(provider), state)
    return RedirectResponse(url)


@router.get("/{provider}/callback")
async def callback(
    provider: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    _require_supported_provider(provider)
    failure = RedirectResponse(
        f"{settings.frontend_base_url}/connectors.html?connector={provider}&status=error"
    )

    if error or not code or not state or not verify_oauth_state_token(state, provider):
        return failure

    try:
        token_response = await google_oauth.exchange_code(code, _redirect_uri(provider))
        userinfo = await google_oauth.fetch_userinfo(token_response.access_token)
    except httpx.HTTPError:
        return failure

    # Google's consent screen lists each permission as a checkbox, unticked by default; a
    # user who just clicks Continue grants nothing but their email. Saving that would look
    # connected while every sync and send fails with 403, so refuse it and say why. Any
    # existing (working) connection is left untouched.
    granted = set((token_response.scope or "").split())
    if missing_permissions(provider, granted):
        await google_oauth.revoke_token(token_response.access_token)
        return RedirectResponse(
            f"{settings.frontend_base_url}/connectors.html"
            f"?connector={provider}&status=missing_permissions"
        )

    existing = await db.execute(
        select(Connector).where(
            Connector.user_id == current_user.id, Connector.provider == provider
        )
    )
    connector = existing.scalar_one_or_none() or Connector(
        user_id=current_user.id, provider=provider
    )
    if connector.external_account_email != userinfo.get("email"):
        # A different Google account: the old one's sync cursor means nothing here.
        connector.sync_state = {}
    connector.external_account_email = userinfo.get("email")
    connector.access_token_encrypted = encrypt_token(token_response.access_token)
    if token_response.refresh_token:
        connector.refresh_token_encrypted = encrypt_token(token_response.refresh_token)
    connector.scopes = token_response.scope or ""
    connector.token_expires_at = token_response.expires_at
    db.add(connector)
    await db.commit()

    return RedirectResponse(
        f"{settings.frontend_base_url}/connectors.html?connector={provider}&status=connected"
    )


@router.delete("/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(
    provider: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    _require_supported_provider(provider)
    connector = (
        await db.execute(
            select(Connector).where(
                Connector.user_id == current_user.id, Connector.provider == provider
            )
        )
    ).scalar_one_or_none()
    if connector is None:
        return
    # End the grant on Google's side too, so disconnecting really withdraws access (best
    # effort: the local tokens are deleted either way). Memories already synced are kept.
    token = connector.refresh_token_encrypted or connector.access_token_encrypted
    await google_oauth.revoke_token(decrypt_token(token))
    await db.execute(delete(Connector).where(Connector.id == connector.id))
    await db.commit()
