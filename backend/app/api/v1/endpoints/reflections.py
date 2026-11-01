import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.conversation import Conversation, ConversationMessage
from app.models.memory_item import MemoryItem, is_first_chunk
from app.models.reflection import Reflection
from app.models.user import User
from app.schemas.reflection import DiscussResponse, GenerateRequest, ReflectionList, ReflectionRead
from app.services.chat.groq_chat import resolve_timezone
from app.services.graph.build import user_addresses
from app.services.reflections import (
    GENERATE_COOLDOWN,
    MIN_MEMORIES,
    WEEKLY,
    GenerationFailed,
    NotEnoughMemories,
    generate_reflections,
    latest_batch_at,
)

router = APIRouter()


async def _listing(user: User, db: AsyncSession) -> ReflectionList:
    reflections = (
        await db.execute(
            select(Reflection)
            .where(Reflection.user_id == user.id, Reflection.dismissed_at.is_(None))
            .order_by(Reflection.created_at.desc(), Reflection.id)
        )
    ).scalars()
    memory_count = await db.scalar(
        select(func.count())
        .select_from(MemoryItem)
        .where(MemoryItem.user_id == user.id, is_first_chunk())
    )
    last = await latest_batch_at(user.id, db)
    cooldown_until = last + GENERATE_COOLDOWN if last else None
    return ReflectionList(
        reflections=[ReflectionRead.model_validate(r) for r in reflections],
        memory_count=memory_count or 0,
        min_memories=MIN_MEMORIES,
        last_generated_at=last,
        next_scheduled_at=last + WEEKLY if last else None,
        can_generate_at=cooldown_until
        if cooldown_until and cooldown_until > datetime.now(UTC)
        else None,
    )


async def _owned(reflection_id: uuid.UUID, user: User, db: AsyncSession) -> Reflection:
    reflection = await db.get(Reflection, reflection_id)
    if reflection is None or reflection.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reflection not found")
    return reflection


@router.get("/", response_model=ReflectionList)
async def list_reflections(
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> ReflectionList:
    """Active (not dismissed) reflections, newest batch first, plus scheduling state."""
    return await _listing(current_user, db)


@router.post("/generate", response_model=ReflectionList)
async def generate_now(
    payload: GenerateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReflectionList:
    """Runs a batch now instead of waiting for the weekly job. Rate-limited per user."""
    last = await latest_batch_at(current_user.id, db)
    if last and datetime.now(UTC) - last < GENERATE_COOLDOWN:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Reflections were just refreshed. Try again in a few minutes.",
        )
    if payload.timezone:
        current_user.timezone = resolve_timezone(payload.timezone)
        await db.commit()
    try:
        await generate_reflections(current_user, db, await user_addresses(current_user, db))
    except NotEnoughMemories:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Your twin needs at least {MIN_MEMORIES} memories before it can spot patterns.",
        ) from None
    except GenerationFailed:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Couldn't generate reflections right now. Try again in a moment.",
        ) from None
    return await _listing(current_user, db)


@router.post("/{reflection_id}/dismiss", response_model=ReflectionRead)
async def dismiss(
    reflection_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Reflection:
    reflection = await _owned(reflection_id, current_user, db)
    reflection.dismissed_at = reflection.dismissed_at or datetime.now(UTC)
    await db.commit()
    await db.refresh(reflection)
    return reflection


@router.post("/{reflection_id}/restore", response_model=ReflectionRead)
async def restore(
    reflection_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Reflection:
    """Undo for a dismiss."""
    reflection = await _owned(reflection_id, current_user, db)
    reflection.dismissed_at = None
    await db.commit()
    await db.refresh(reflection)
    return reflection


@router.post("/{reflection_id}/discuss", response_model=DiscussResponse)
async def discuss(
    reflection_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DiscussResponse:
    """'Talk it through': opens a chat thread that starts with this reflection (and its cited
    memories), so the conversation has the context. Reuses the thread on later clicks."""
    reflection = await _owned(reflection_id, current_user, db)
    if reflection.conversation_id is not None:
        existing = await db.get(Conversation, reflection.conversation_id)
        if existing is not None:
            return DiscussResponse(conversation_id=existing.id)

    conversation = Conversation(user_id=current_user.id, title=reflection.headline[:200])
    db.add(conversation)
    await db.flush()
    db.add(
        ConversationMessage(
            conversation_id=conversation.id,
            role="assistant",
            content=(
                f"Here's something I noticed: {reflection.body}\n\n"
                "What would you like to dig into? I can pull up the memories behind it, "
                "or look for related ones."
            ),
            citations=[
                {
                    "source": e["source"],
                    "external_id": e["external_id"],
                    "title": e["title"],
                    "snippet": e.get("snippet", ""),
                    "occurred_at": e["occurred_at"],
                }
                for e in reflection.evidence
            ],
        )
    )
    reflection.conversation_id = conversation.id
    await db.commit()
    return DiscussResponse(conversation_id=conversation.id)
