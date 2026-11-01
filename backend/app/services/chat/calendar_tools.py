"""Google Calendar tools for the chat twin.

Read tools (list_calendars, list_calendar_events, find_free_time) query Google live and return
their results to the model straight away. Write tools only *prepare* a draft here — fetching
the event being changed so the user sees exactly what will happen — and the draft is executed
later by the confirm endpoint (google_calendar_events.execute).
"""

import json
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connector import Connector
from app.services.actions import google_calendar_events as gcal
from app.services.oauth.tokens import ConnectorAccessExpired, get_valid_access_token

READ_TOOLS = ("list_calendars", "list_calendar_events", "find_free_time")
WRITE_TOOLS = gcal.WRITE_TOOLS
ALL_TOOLS = (*READ_TOOLS, *WRITE_TOOLS)

_DEFAULT_WINDOW = timedelta(days=7)
_SEARCH_WINDOW = timedelta(days=365)
_MAX_EVENTS = 100
_MAX_SLOTS = 30
_MAX_FREE_TIME_DAYS = 31

# Kept terse on purpose: every chat request carries all of this, and Groq's free tier allows
# only a few thousand tokens a minute. Behavior rules live here once, not in each schema.
RULES = (
    " Calendar: for any schedule question (a day/week, past events, far-future events, "
    "finding a meeting) call list_calendar_events — it is live, covers all dates and all the "
    "user's calendars, and beats search_memory for calendar questions. 'When am I free' → "
    "find_free_time. A named calendar ('work calendar') → list_calendars for its id. Results "
    "of these three tools are NOT shown to the user: put what matters in your reply as a short "
    "bullet list (weekday date, time, title); copy the 'day' labels, never compute weekdays."
    " Creating: resolve dates against today; a date without a year is its next occurrence, a "
    "weekday name the coming one. Birthdays/anniversaries → all-day, yearly, titled like "
    "\"Neha's Birthday\". No time of day → all-day. No duration → omit end (1 hour). 'every "
    "Monday' → weekly. 'remind me 30 min before' → reminder_minutes=[30] (a day = 1440). "
    "Invite attendees only when asked, by email (search_memory to find one)."
    " Changing/moving/cancelling/deleting/accepting/declining an EXISTING event: find it with "
    "list_calendar_events first and use its event_id and calendar_id — never guess ids. If "
    "several match, take the obvious one (usually the next upcoming). Recurring events: "
    "apply_to='this' unless the user says every/all/series. Pass only the fields that change; "
    "a moved event keeps its length."
)

# --- tool schemas ---------------------------------------------------------------------

_STR = {"type": ["string", "null"]}
_WHEN = "local 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM'"
_EVENT_REF = {
    "event_id": {"type": "string", "description": "id from list_calendar_events"},
    "calendar_id": {**_STR, "description": "calendar_id from list_calendar_events"},
    "apply_to": {"type": ["string", "null"], "enum": [*gcal.APPLY_TO, None]},
}
_REMINDERS = {
    "type": ["array", "null"],
    "items": {"type": "integer"},
    "description": "popup reminders, minutes before start; [] = none",
}
_EMAILS = {"type": ["array", "null"], "items": {"type": "string"}}


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_calendars": _tool("list_calendars", "List the user's Google calendars.", {}, []),
    "list_calendar_events": _tool(
        "list_calendar_events",
        "Read the user's Google Calendar events live, any date range, all calendars. "
        "All-day 'end' is the last day, inclusive.",
        {
            "time_min": {**_STR, "description": f"{_WHEN}; default now"},
            "time_max": {**_STR, "description": f"{_WHEN}; a date includes that day"},
            "query": {**_STR, "description": "text search; alone it spans ±1 year"},
            "calendar_id": {**_STR, "description": "one calendar only"},
            "limit": {"type": ["integer", "null"]},
        },
        [],
    ),
    "find_free_time": _tool(
        "find_free_time",
        "Find open slots across the user's calendars within working hours.",
        {
            "time_min": {"type": "string", "description": _WHEN},
            "time_max": {**_STR, "description": f"{_WHEN}; default end of time_min's day"},
            "duration_minutes": {"type": ["integer", "null"], "description": "default 30"},
            "day_start": {**_STR, "description": "'HH:MM', default 09:00"},
            "day_end": {**_STR, "description": "'HH:MM', default 18:00"},
            "include_weekends": {"type": ["boolean", "null"]},
        },
        ["time_min"],
    ),
    "create_calendar_event": _tool(
        "create_calendar_event",
        "Draft a new Google Calendar event (added only after the user confirms). Use for "
        "add/schedule/book/remind-me requests.",
        {
            "summary": {"type": "string"},
            "start": {
                "type": "string",
                "description": "'YYYY-MM-DD' (all-day) or local " "'YYYY-MM-DDTHH:MM'",
            },
            "end": {**_STR, "description": "same format; all-day = last day, inclusive"},
            "all_day": {"type": ["boolean", "null"]},
            "recurrence": {"type": ["string", "null"], "enum": [*gcal.RECURRENCES, None]},
            "reminder_minutes": _REMINDERS,
            "location": _STR,
            "description": _STR,
            "attendees": {**_EMAILS, "description": "emails, only if asked"},
            "calendar_id": {**_STR, "description": "from list_calendars; default primary"},
        },
        ["summary", "start"],
    ),
    "update_calendar_event": _tool(
        "update_calendar_event",
        "Draft a change to an existing event (move, rename, place, repeat, reminders, guests); "
        "applied only after the user confirms. Pass only changed fields.",
        {
            **_EVENT_REF,
            "start": {**_STR, "description": f"{_WHEN}; a bare date keeps the time"},
            "end": _STR,
            "all_day": {"type": ["boolean", "null"]},
            "summary": _STR,
            "location": _STR,
            "description": _STR,
            "recurrence": {
                "type": ["string", "null"],
                "enum": [*gcal.RECURRENCES, "none", None],
                "description": "needs apply_to='series'",
            },
            "reminder_minutes": _REMINDERS,
            "add_attendees": _EMAILS,
            "remove_attendees": _EMAILS,
        },
        ["event_id"],
    ),
    "delete_calendar_event": _tool(
        "delete_calendar_event",
        "Draft deleting/cancelling an event; done only after the user confirms.",
        dict(_EVENT_REF),
        ["event_id"],
    ),
    "respond_to_invite": _tool(
        "respond_to_invite",
        "Draft an RSVP to an invite (tentative = maybe); sent only after the user confirms.",
        {**_EVENT_REF, "response": {"type": "string", "enum": list(gcal.RSVP_RESPONSES)}},
        ["event_id", "response"],
    ),
}


# --- per-turn session -----------------------------------------------------------------


class CalendarSession:
    """Lazily loads the user's Calendar token (and calendar list) at most once per chat turn."""

    def __init__(self, user_id: uuid.UUID, db: AsyncSession, timezone: str) -> None:
        self.user_id = user_id
        self.db = db
        self.timezone = timezone
        self._connector: Connector | None = None
        self._token: str | None = None
        self._calendars: list[dict[str, Any]] | None = None

    async def token(self) -> str:
        if self._token is None:
            self._connector = (
                await self.db.execute(
                    select(Connector).where(
                        Connector.user_id == self.user_id,
                        Connector.provider == "google_calendar",
                    )
                )
            ).scalar_one_or_none()
            if self._connector is None:
                raise ValueError("Google Calendar isn't connected")
            self._token = await get_valid_access_token(self._connector, self.db)
        return self._token

    async def can_write(self) -> bool:
        await self.token()
        return self._connector is not None and gcal.can_write(self._connector.scopes)

    async def calendars(self) -> list[dict[str, Any]]:
        if self._calendars is None:
            self._calendars = await gcal.list_calendars(await self.token())
        return self._calendars

    async def resolve_calendar(self, value: str | None) -> dict[str, Any]:
        """A calendar by id or (case-insensitive) name; None/'primary' → the primary one."""
        if not value or value == "primary":
            return {"id": "primary", "name": None}
        calendars = await self.calendars()
        wanted = value.strip().lower()
        for calendar in calendars:
            if calendar["id"].lower() == wanted or calendar["name"].lower() == wanted:
                return calendar
        names = ", ".join(c["name"] for c in calendars)
        raise ValueError(f"no calendar called {value!r} — the user's calendars are: {names}")


# --- helpers --------------------------------------------------------------------------


def _for_model(summary: dict[str, Any], calendar_name: str | None) -> dict[str, Any]:
    """Event summary as the model sees it: all-day ends made inclusive, noise dropped."""
    view = {k: v for k, v in summary.items() if v not in (None, [], False) and k != "html_link"}
    if summary["all_day"] and summary.get("end"):
        last = date.fromisoformat(summary["end"]) - timedelta(days=1)
        view["end"] = last.isoformat()
    view["all_day"] = summary["all_day"]
    if summary.get("start"):
        # Models are unreliable at weekday arithmetic — hand them the label.
        view["day"] = datetime.fromisoformat(summary["start"]).strftime("%a %d %b %Y")
    if calendar_name:
        view["calendar"] = calendar_name
    if len(view.get("attendees") or []) > 10:
        view["attendees"] = view["attendees"][:10] + [{"more": len(summary["attendees"]) - 10}]
    return view


def _clock(value: Any, default: time) -> time:
    if not value:
        return default
    try:
        return time.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"times of day must be 'HH:MM', got {value!r}") from exc


def _http_reason(exc: httpx.HTTPStatusError) -> str:
    code = exc.response.status_code
    if code == 404:
        return "Google has no such event or calendar — find it with list_calendar_events first"
    if code in (401, 403):
        return (
            "Google refused access — the user may need to reconnect Google Calendar on the "
            "Connectors page"
        )
    return f"Google Calendar returned HTTP {code}"


# --- read tools -----------------------------------------------------------------------


async def _list_calendars(session: CalendarSession, args: dict[str, Any]) -> Any:
    return {"calendars": await session.calendars()}


async def _list_events(session: CalendarSession, args: dict[str, Any]) -> Any:
    tz = session.timezone
    now = datetime.now(ZoneInfo(tz))
    query = (args.get("query") or "").strip() or None
    time_min = gcal.parse_bound(args["time_min"], tz) if args.get("time_min") else None
    time_max = gcal.parse_bound(args["time_max"], tz, is_end=True) if args.get("time_max") else None
    if time_min is None and time_max is None and query:
        time_min, time_max = now - _SEARCH_WINDOW, now + _SEARCH_WINDOW
    time_min = time_min or (
        now if time_max is None or time_max > now else time_max - _DEFAULT_WINDOW
    )
    time_max = time_max or time_min + _DEFAULT_WINDOW
    if time_max <= time_min:
        raise ValueError("time_max must be after time_min")
    limit = min(int(args.get("limit") or 25), _MAX_EVENTS)

    if args.get("calendar_id"):
        calendars = [await session.resolve_calendar(args["calendar_id"])]
    else:
        calendars = [c for c in await session.calendars() if c["shown"]] or [
            {"id": "primary", "name": None}
        ]
    token = await session.token()
    events: list[tuple[str, dict[str, Any]]] = []
    for calendar in calendars:
        for event in await gcal.list_events(
            token, calendar["id"], time_min, time_max, tz, query=query, limit=limit
        ):
            summary = gcal.summarize_event(event, calendar["id"], tz)
            events.append((summary["start"] or "", _for_model(summary, calendar.get("name"))))
    events.sort(key=lambda pair: pair[0])
    return {
        "range": {"from": time_min.isoformat(), "to": time_max.isoformat()},
        "query": query,
        "count": len(events),
        "events": [view for _, view in events[:limit]],
        "truncated": len(events) > limit,
    }


async def _find_free_time(session: CalendarSession, args: dict[str, Any]) -> Any:
    tz_name = session.timezone
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    time_min = gcal.parse_bound(str(args["time_min"]), tz_name)
    time_max = (
        gcal.parse_bound(str(args["time_max"]), tz_name, is_end=True)
        if args.get("time_max")
        else datetime.combine(time_min.date() + timedelta(days=1), time.min, tz)
    )
    if time_max <= time_min:
        raise ValueError("time_max must be after time_min")
    if (time_max - time_min).days > _MAX_FREE_TIME_DAYS:
        raise ValueError(f"search at most {_MAX_FREE_TIME_DAYS} days at a time")
    duration = timedelta(minutes=int(args.get("duration_minutes") or 30))
    day_start = _clock(args.get("day_start"), time(9, 0))
    day_end = _clock(args.get("day_end"), time(18, 0))
    last_day = (time_max - timedelta(microseconds=1)).date()  # time_max itself is exclusive
    days = [
        time_min.date() + timedelta(days=i) for i in range((last_day - time_min.date()).days + 1)
    ]
    weekends = args.get("include_weekends")
    if weekends is None:  # a range that is only weekend days obviously means them
        weekends = all(d.weekday() >= 5 for d in days)

    calendar_ids = [c["id"] for c in await session.calendars() if c["shown"]] or ["primary"]
    busy = await gcal.busy_blocks(await session.token(), calendar_ids, time_min, time_max, tz_name)

    slots = []
    for day in days:
        if day.weekday() >= 5 and not weekends:
            continue
        cursor = max(datetime.combine(day, day_start, tz), time_min, now)
        window_end = min(datetime.combine(day, day_end, tz), time_max)
        for busy_start, busy_end in busy:
            if busy_end <= cursor or busy_start >= window_end:
                continue
            if busy_start - cursor >= duration:
                slots.append((cursor, busy_start))
            cursor = max(cursor, busy_end)
        if window_end - cursor >= duration:
            slots.append((cursor, window_end))

    return {
        "timezone": tz_name,
        "slot_minutes_at_least": int(duration.total_seconds() // 60),
        "working_hours": f"{day_start:%H:%M}-{day_end:%H:%M}",
        "free_slots": [
            {
                "day": start.strftime("%a %d %b"),
                "from": start.strftime("%Y-%m-%dT%H:%M"),
                "to": end.strftime("%Y-%m-%dT%H:%M"),
            }
            for start, end in slots[:_MAX_SLOTS]
        ],
        "busy_block_count": len(busy),
    }


_READERS = {
    "list_calendars": _list_calendars,
    "list_calendar_events": _list_events,
    "find_free_time": _find_free_time,
}


async def run_read_tool(name: str, args: dict[str, Any], session: CalendarSession) -> str:
    try:
        result = await _READERS[name](session, args)
    except (ValueError, ConnectorAccessExpired) as exc:
        return json.dumps({"status": "error", "message": str(exc)})
    except httpx.HTTPStatusError as exc:
        return json.dumps({"status": "error", "message": _http_reason(exc)})
    except (httpx.HTTPError, RuntimeError):
        return json.dumps({"status": "error", "message": "Couldn't reach Google Calendar"})
    return json.dumps(result, default=str)


# --- write drafts ---------------------------------------------------------------------


async def _target_event(
    session: CalendarSession, args: dict[str, Any]
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    """(calendar_id, the picked event, its recurring series if apply_to='series')."""
    event_id = str(args.get("event_id") or "").strip()
    if not event_id:
        raise ValueError("'event_id' is required — find it with list_calendar_events")
    calendar = await session.resolve_calendar(args.get("calendar_id"))
    token = await session.token()
    event = await gcal.get_event(token, calendar["id"], event_id, session.timezone)
    if event.get("status") == "cancelled":
        raise ValueError("that event is already cancelled")
    master = None
    if gcal.resolve_apply_to(args, event) == "series":
        master = await gcal.get_event(
            token, calendar["id"], event["recurringEventId"], session.timezone
        )
    return calendar["id"], event, master


async def prepare_draft(name: str, args: dict[str, Any], session: CalendarSession) -> dict:
    """The payload the user will confirm, or ValueError telling the model what to fix."""
    try:
        if name == "create_calendar_event":
            calendar = await session.resolve_calendar(args.get("calendar_id"))
            return gcal.build_event_payload(
                {**args, "calendar_id": calendar["id"], "calendar_name": calendar.get("name")},
                session.timezone,
            )
        calendar_id, event, master = await _target_event(session, args)
        if name == "update_calendar_event":
            return gcal.build_update_payload(event, master, args, calendar_id, session.timezone)
        if name == "delete_calendar_event":
            return gcal.build_delete_payload(event, master, calendar_id, session.timezone)
        return gcal.build_rsvp_payload(
            event, master, str(args.get("response") or ""), calendar_id, session.timezone
        )
    except ConnectorAccessExpired as exc:
        raise ValueError(str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        raise ValueError(_http_reason(exc)) from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        raise ValueError("Couldn't reach Google Calendar — try again") from exc
