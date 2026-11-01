from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any


@dataclass
class NormalizedItem:
    external_id: str
    content: str
    item_metadata: dict[str, Any]
    # When the thing itself happened (email sent, event starts) — drives the timeline.
    # None means "unknown"; storage falls back to the time it was indexed.
    occurred_at: datetime | None = None


@dataclass
class SyncBatch:
    """One poll's worth of items, plus the cursor the next poll should resume from."""

    items: list[NormalizedItem]
    sync_state: dict[str, Any]


def parse_email_date(value: str | None) -> datetime | None:
    """RFC 2822 `Date:` header → aware datetime; None if missing or malformed."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_event_start(value: str | None) -> tuple[datetime | None, bool]:
    """Calendar start (`dateTime` ISO string, or `date` for all-day) → (datetime, all_day)."""
    if not value:
        return None, False
    try:
        if "T" not in value:
            day = date.fromisoformat(value)
            return datetime(day.year, day.month, day.day, tzinfo=UTC), True
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, False
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)), False
