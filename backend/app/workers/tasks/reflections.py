import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.db.session import async_session_factory
from app.models.memory_item import MemoryItem
from app.models.reflection import Reflection
from app.models.user import User
from app.services.graph.build import user_addresses
from app.services.reflections import MIN_MEMORIES, WEEKLY, NotEnoughMemories, generate_reflections
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="generate_due_reflections")
def generate_due_reflections() -> dict[str, int | str]:
    return asyncio.run(_generate_due_async())


async def _generate_due_async(only: set[uuid.UUID] | None = None) -> dict[str, int | str]:
    """Runs daily: any user with enough memories whose last batch is a week old (or who has
    never had one) gets a fresh batch. Each user in their own session, failures isolated.
    `only` narrows the sweep to specific users (tests share a database with real accounts)."""
    now = datetime.now(UTC)
    async with async_session_factory() as db:
        last_batch = (
            select(Reflection.user_id, func.max(Reflection.created_at).label("last"))
            .group_by(Reflection.user_id)
            .subquery()
        )
        memory_counts = (
            select(MemoryItem.user_id, func.count().label("n"))
            .group_by(MemoryItem.user_id)
            .subquery()
        )
        due = (
            await db.execute(
                select(User.id)
                .join(memory_counts, memory_counts.c.user_id == User.id)
                .outerjoin(last_batch, last_batch.c.user_id == User.id)
                .where(
                    User.is_active.is_(True),
                    memory_counts.c.n >= MIN_MEMORIES,
                    (last_batch.c.last.is_(None)) | (last_batch.c.last <= now - WEEKLY),
                    *([User.id.in_(only)] if only is not None else []),
                )
            )
        ).scalars()
        user_ids = list(due)

    results: dict[str, int | str] = {}
    for user_id in user_ids:
        async with async_session_factory() as db:
            user = await db.get(User, user_id)
            if user is None:
                continue
            try:
                created = await generate_reflections(user, db, await user_addresses(user, db))
                results[str(user_id)] = len(created)
            except NotEnoughMemories:
                results[str(user_id)] = "not_enough_memories"
            except Exception:
                logger.exception("generate_due_reflections failed for %s", user_id)
                results[str(user_id)] = "error"
    return results
