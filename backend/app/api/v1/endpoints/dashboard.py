from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.connector import Connector
from app.models.memory_item import MemoryItem, is_first_chunk
from app.models.user import User
from app.schemas.dashboard import ActivityItem, DashboardSummary, MemoryStats, SourceStats
from app.services.oauth import google as google_oauth

router = APIRouter()

_ACTIVITY_LIMIT = 5


@router.get("/", response_model=DashboardSummary)
async def get_summary(
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> DashboardSummary:
    now = datetime.now(UTC)
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)
    created = MemoryItem.created_at

    stats = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(created >= start_of_today),
                func.count().filter(created >= week_ago),
                func.count().filter(created >= two_weeks_ago, created < week_ago),
                func.count(MemoryItem.embedding),
                func.max(created),
            ).where(MemoryItem.user_id == current_user.id)
        )
    ).one()
    total, today, this_week, last_week, searchable, last_indexed_at = stats

    connectors = (
        (await db.execute(select(Connector).where(Connector.user_id == current_user.id)))
        .scalars()
        .all()
    )

    # There's no sync log yet, so "sync" activity is derived from memory_items: one row
    # per (source, UTC day) with how many items were first indexed that day. An uploaded
    # file counts once, not once per chunk.
    day = cast(created, Date)
    sync_rows = await db.execute(
        select(MemoryItem.source, func.count(), func.max(created))
        .where(MemoryItem.user_id == current_user.id, is_first_chunk())
        .group_by(MemoryItem.source, day)
        .order_by(func.max(created).desc())
        .limit(_ACTIVITY_LIMIT)
    )
    activity = [
        ActivityItem(kind="sync", source=source, count=count, at=at)
        for source, count, at in sync_rows
    ]
    activity += [
        ActivityItem(kind="connected", source=c.provider, count=1, at=c.created_at)
        for c in connectors
    ]
    activity.sort(key=lambda a: a.at, reverse=True)

    return DashboardSummary(
        email=current_user.email,
        full_name=current_user.full_name,
        memories=MemoryStats(
            total=total,
            today=today,
            this_week=this_week,
            last_week=last_week,
            searchable=searchable,
        ),
        sources=SourceStats(connected=len(connectors), available=len(google_oauth.PROVIDER_SCOPES)),
        last_indexed_at=last_indexed_at,
        activity=activity[:_ACTIVITY_LIMIT],
    )
