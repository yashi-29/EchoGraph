import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.crypto import decrypt_token
from app.core.security import hash_password, verify_password
from app.db.session import get_db
from app.models.connector import Connector
from app.models.conversation import Conversation, ConversationMessage
from app.models.memory_item import MemoryItem
from app.models.pending_action import PendingAction
from app.models.reflection import Reflection
from app.models.upload import UPLOAD_KINDS, Upload
from app.models.user import User
from app.schemas.account import DataSummary, DeletedCount, SourceDataSummary
from app.schemas.user import (
    AccountDeletion,
    PasswordChange,
    PreferencesUpdate,
    ProfileUpdate,
    UserRead,
)
from app.services.oauth import google as google_oauth

router = APIRouter()
settings = get_settings()


def _require_password(user: User, password: str | None) -> None:
    if not password or not verify_password(password, user.hashed_password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is incorrect")


# --- profile & preferences ---------------------------------------------------------------


@router.patch("/profile", response_model=UserRead)
async def update_profile(
    payload: ProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    if "full_name" in payload.model_fields_set:
        current_user.full_name = (payload.full_name or "").strip() or None

    new_email = payload.email.lower() if payload.email else None
    if new_email and new_email != current_user.email:
        # The email is the login identifier, so changing it needs the password.
        _require_password(current_user, payload.current_password)
        current_user.email = new_email

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "An account with that email already exists"
        ) from exc
    await db.refresh(current_user)
    return current_user


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: PasswordChange,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    _require_password(current_user, payload.current_password)
    current_user.hashed_password = hash_password(payload.new_password)
    await db.commit()


@router.patch("/preferences", response_model=UserRead)
async def update_preferences(
    payload: PreferencesUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    sent = payload.model_fields_set
    if "allow_content_in_chat" in sent and payload.allow_content_in_chat is not None:
        current_user.allow_content_in_chat = payload.allow_content_in_chat
    if "response_tone" in sent and payload.response_tone is not None:
        current_user.response_tone = payload.response_tone
    if "memory_window_days" in sent:
        current_user.memory_window_days = payload.memory_window_days
    await db.commit()
    await db.refresh(current_user)
    return current_user


# --- your data ---------------------------------------------------------------------------


@router.get("/data", response_model=DataSummary)
async def data_summary(
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> DataSummary:
    memory_rows = await db.execute(
        select(
            MemoryItem.source,
            func.count(),
            func.min(MemoryItem.occurred_at),
            func.max(MemoryItem.occurred_at),
        )
        .where(MemoryItem.user_id == current_user.id)
        .group_by(MemoryItem.source)
    )
    by_source = {source: (count, oldest, newest) for source, count, oldest, newest in memory_rows}
    connectors = {
        c.provider: c
        for c in (
            await db.execute(select(Connector).where(Connector.user_id == current_user.id))
        ).scalars()
    }

    sources = []
    for source in sorted(set(by_source) | set(connectors)):
        count, oldest, newest = by_source.get(source, (0, None, None))
        connector = connectors.get(source)
        sources.append(
            SourceDataSummary(
                source=source,
                memories=count,
                connected=connector is not None,
                account_email=connector.external_account_email if connector else None,
                oldest=oldest,
                newest=newest,
            )
        )

    conversations, messages = (
        await db.execute(
            select(
                func.count(func.distinct(Conversation.id)),
                func.count(ConversationMessage.id),
            )
            .select_from(Conversation)
            .outerjoin(ConversationMessage, ConversationMessage.conversation_id == Conversation.id)
            .where(Conversation.user_id == current_user.id)
        )
    ).one()

    return DataSummary(
        total_memories=sum(count for count, _, _ in by_source.values()),
        conversations=conversations,
        messages=messages,
        sources=sources,
    )


@router.get("/export")
async def export_data(
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Response:
    """Everything stored for this user as one JSON download. Excludes secrets (password hash,
    OAuth tokens) and embeddings (opaque vectors, not the user's data).
    """
    memories = (
        await db.execute(
            select(MemoryItem)
            .where(MemoryItem.user_id == current_user.id)
            .order_by(MemoryItem.occurred_at)
        )
    ).scalars()
    conversations = (
        await db.execute(
            select(Conversation)
            .where(Conversation.user_id == current_user.id)
            .order_by(Conversation.created_at)
        )
    ).scalars()
    conversation_list = list(conversations)
    messages_by_conversation: dict = {c.id: [] for c in conversation_list}
    if conversation_list:
        for message in (
            await db.execute(
                select(ConversationMessage)
                .where(ConversationMessage.conversation_id.in_(messages_by_conversation))
                .order_by(ConversationMessage.seq)
            )
        ).scalars():
            messages_by_conversation[message.conversation_id].append(message)
    connectors = (
        await db.execute(select(Connector).where(Connector.user_id == current_user.id))
    ).scalars()
    uploads = (
        await db.execute(
            select(Upload).where(Upload.user_id == current_user.id).order_by(Upload.created_at)
        )
    ).scalars()
    actions = (
        await db.execute(
            select(PendingAction)
            .where(PendingAction.user_id == current_user.id)
            .order_by(PendingAction.created_at)
        )
    ).scalars()

    reflections = (
        await db.execute(
            select(Reflection)
            .where(Reflection.user_id == current_user.id)
            .order_by(Reflection.created_at)
        )
    ).scalars()

    export = {
        "exported_at": datetime.now(UTC).isoformat(),
        "account": {
            "email": current_user.email,
            "full_name": current_user.full_name,
            "created_at": current_user.created_at.isoformat(),
            "preferences": {
                "allow_content_in_chat": current_user.allow_content_in_chat,
                "response_tone": current_user.response_tone,
                "memory_window_days": current_user.memory_window_days,
            },
        },
        "connected_sources": [
            {
                "provider": c.provider,
                "account_email": c.external_account_email,
                "connected_at": c.created_at.isoformat(),
            }
            for c in connectors
        ],
        # File metadata only — the originals can be downloaded one by one from Uploads.
        "uploads": [
            {
                "kind": u.kind,
                "filename": u.filename,
                "content_type": u.content_type,
                "size_bytes": u.size_bytes,
                "sha256": u.sha256,
                "uploaded_at": u.created_at.isoformat(),
            }
            for u in uploads
        ],
        "memories": [
            {
                "source": m.source,
                "external_id": m.external_id,
                "occurred_at": m.occurred_at.isoformat(),
                "indexed_at": m.created_at.isoformat(),
                "content": m.content,
                "metadata": m.item_metadata,
            }
            for m in memories
        ],
        "conversations": [
            {
                "title": c.title,
                "created_at": c.created_at.isoformat(),
                "messages": [
                    {
                        "role": m.role,
                        "content": m.content,
                        "citations": m.citations,
                        "created_at": m.created_at.isoformat(),
                    }
                    for m in messages_by_conversation[c.id]
                ],
            }
            for c in conversation_list
        ],
        "reflections": [
            {
                "headline": r.headline,
                "observation": r.body,
                "category": r.category,
                "tone": r.tone,
                "tags": r.tags,
                "evidence": r.evidence,
                "dismissed": r.dismissed_at is not None,
                "created_at": r.created_at.isoformat(),
            }
            for r in reflections
        ],
        "actions": [
            {
                "tool": a.tool_name,
                "status": a.status,
                "payload": a.payload,
                "created_at": a.created_at.isoformat(),
            }
            for a in actions
        ],
    }
    filename = f"digital-twin-export-{datetime.now(UTC):%Y-%m-%d}.json"
    return Response(
        content=json.dumps(export, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/memories", response_model=DeletedCount)
async def forget_memories(
    source: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DeletedCount:
    """Deletes indexed memories (all, or one source's). The connector stays connected, so a
    later sync re-indexes whatever the provider still returns — disconnect it to stop that.
    """
    stmt = delete(MemoryItem).where(MemoryItem.user_id == current_user.id)
    if source:
        stmt = stmt.where(MemoryItem.source == source)
    result = await db.execute(stmt)
    # Forgetting uploaded memories also deletes the original files — keeping a copy of
    # something the user asked the twin to forget would defeat the point.
    if not source or source in UPLOAD_KINDS:
        uploads = delete(Upload).where(Upload.user_id == current_user.id)
        if source:
            uploads = uploads.where(Upload.kind == source)
        await db.execute(uploads)
    await db.commit()
    return DeletedCount(deleted=result.rowcount or 0)  # type: ignore[attr-defined]


@router.delete("/conversations", response_model=DeletedCount)
async def clear_chat_history(
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> DeletedCount:
    result = await db.execute(delete(Conversation).where(Conversation.user_id == current_user.id))
    await db.commit()
    return DeletedCount(deleted=result.rowcount or 0)  # type: ignore[attr-defined]


# --- danger zone -------------------------------------------------------------------------


async def _wipe_twin(user: User, db: AsyncSession) -> None:
    """Deletes everything the twin learned or holds for this user, keeping the login itself.
    Google grants are revoked first (best effort) so no stale access lingers on Google's side.
    """
    connectors = (await db.execute(select(Connector).where(Connector.user_id == user.id))).scalars()
    for connector in connectors:
        token = connector.refresh_token_encrypted or connector.access_token_encrypted
        await google_oauth.revoke_token(decrypt_token(token))

    # Order matters for foreign keys: reflections point at conversations, and messages
    # (cascade with conversations) reference actions.
    await db.execute(delete(Reflection).where(Reflection.user_id == user.id))
    await db.execute(delete(Conversation).where(Conversation.user_id == user.id))
    await db.execute(delete(PendingAction).where(PendingAction.user_id == user.id))
    await db.execute(delete(MemoryItem).where(MemoryItem.user_id == user.id))
    await db.execute(delete(Upload).where(Upload.user_id == user.id))
    await db.execute(delete(Connector).where(Connector.user_id == user.id))


@router.post("/reset", status_code=status.HTTP_204_NO_CONTENT)
async def delete_twin(
    payload: AccountDeletion,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    _require_password(current_user, payload.password)
    await _wipe_twin(current_user, db)
    await db.commit()


@router.post("/delete", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    payload: AccountDeletion,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    _require_password(current_user, payload.password)
    await _wipe_twin(current_user, db)
    await db.execute(delete(User).where(User.id == current_user.id))
    await db.commit()
    response.delete_cookie(settings.session_cookie_name)
