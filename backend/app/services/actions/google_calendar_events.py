"""Google Calendar API client for the chat twin.

Reads (list calendars/events, free/busy) run immediately during a chat turn. Everything
that changes a calendar is split in two: a `build_*` / `prepare_*` step that turns the model's
arguments into a complete, reviewable draft (a PendingAction payload), and `execute()`, which
only the confirm endpoint calls — see the safety-critical rule in CONTRIBUTING.md.
"""

from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

_API = "https://www.googleapis.com/calendar/v3"
# Either scope lets us change events; calendar.readonly (the original grant) does not.
_WRITE_SCOPES = {
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar",
}
_RRULES = {
    "daily": "RRULE:FREQ=DAILY",
    "weekly": "RRULE:FREQ=WEEKLY",
    "monthly": "RRULE:FREQ=MONTHLY",
    "yearly": "RRULE:FREQ=YEARLY",
}
RECURRENCES = tuple(_RRULES)
RSVP_RESPONSES = ("accepted", "declined", "tentative")
APPLY_TO = ("this", "series")
_DEFAULT_DURATION = timedelta(hours=1)
_MAX_REMINDER_MINUTES = 40320  # Google's limit: 4 weeks

WRITE_TOOLS = (
    "create_calendar_event",
    "update_calendar_event",
    "delete_calendar_event",
    "respond_to_invite",
)


def can_write(scopes: str) -> bool:
    return bool(_WRITE_SCOPES & set(scopes.split()))


# --- HTTP ---------------------------------------------------------------------------------


def _events_url(calendar_id: str, event_id: str | None = None) -> str:
    url = f"{_API}/calendars/{quote(calendar_id, safe='')}/events"
    return f"{url}/{quote(event_id, safe='')}" if event_id else url


async def _request(
    method: str,
    url: str,
    access_token: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        response = await client.request(
            method,
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
            json=json,
        )
        response.raise_for_status()
        return dict(response.json()) if response.content else {}


async def list_calendars(access_token: str) -> list[dict[str, Any]]:
    data = await _request("GET", f"{_API}/users/me/calendarList", access_token)
    return [
        {
            "id": c["id"],
            "name": c.get("summaryOverride") or c.get("summary") or c["id"],
            "primary": bool(c.get("primary")),
            "access": c.get("accessRole"),
            "shown": c.get("selected", False) or bool(c.get("primary")),
        }
        for c in data.get("items", [])
    ]


async def list_events(
    access_token: str,
    calendar_id: str,
    time_min: datetime,
    time_max: datetime,
    timezone: str,
    query: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": min(max(limit, 1), 250),
        "timeZone": timezone,
    }
    if query:
        params["q"] = query
    data = await _request("GET", _events_url(calendar_id), access_token, params=params)
    return list(data.get("items", []))


async def get_event(
    access_token: str, calendar_id: str, event_id: str, timezone: str
) -> dict[str, Any]:
    return await _request(
        "GET", _events_url(calendar_id, event_id), access_token, params={"timeZone": timezone}
    )


async def busy_blocks(
    access_token: str,
    calendar_ids: list[str],
    time_min: datetime,
    time_max: datetime,
    timezone: str,
) -> list[tuple[datetime, datetime]]:
    data = await _request(
        "POST",
        f"{_API}/freeBusy",
        access_token,
        json={
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "timeZone": timezone,
            "items": [{"id": cid} for cid in calendar_ids],
        },
    )
    blocks = []
    for calendar in data.get("calendars", {}).values():
        for busy in calendar.get("busy", []):
            blocks.append(
                (
                    datetime.fromisoformat(busy["start"].replace("Z", "+00:00")),
                    datetime.fromisoformat(busy["end"].replace("Z", "+00:00")),
                )
            )
    return sorted(blocks)


# --- parsing helpers ----------------------------------------------------------------------


def _parse_local(value: str, field: str, all_day: bool) -> date | datetime:
    """A wall-clock date/time in the user's zone. Any offset the model adds is dropped —
    the event's zone is always the user's, sent separately as `timezone`."""
    try:
        if all_day:
            return date.fromisoformat(value[:10])
        return datetime.fromisoformat(value.replace("Z", "")).replace(tzinfo=None, microsecond=0)
    except ValueError as exc:
        expected = "YYYY-MM-DD" if all_day else "YYYY-MM-DDTHH:MM"
        raise ValueError(f"'{field}' must be {expected}, got {value!r}") from exc


def parse_bound(value: str, timezone: str, *, is_end: bool = False) -> datetime:
    """A tool's time_min/time_max ('YYYY-MM-DD' or local 'YYYY-MM-DDTHH:MM') → aware datetime.
    A bare date as an end bound means the end of that day."""
    tz = ZoneInfo(timezone)
    if "T" not in value:
        try:
            day = date.fromisoformat(value[:10])
        except ValueError as exc:
            raise ValueError(f"expected YYYY-MM-DD or YYYY-MM-DDTHH:MM, got {value!r}") from exc
        if is_end:
            day += timedelta(days=1)
        return datetime.combine(day, time.min, tz)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"expected YYYY-MM-DD or YYYY-MM-DDTHH:MM, got {value!r}") from exc
    return parsed.astimezone(tz) if parsed.tzinfo else parsed.replace(tzinfo=tz)


def _local_str(value: dict[str, Any] | None, timezone: str) -> str | None:
    """A Google start/end object → 'YYYY-MM-DD' (all-day) or local 'YYYY-MM-DDTHH:MM'."""
    if not value:
        return None
    if value.get("date"):
        return str(value["date"])
    raw = value.get("dateTime")
    if not raw:
        return None
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return parsed.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%dT%H:%M")


def _reminder_minutes(args: dict[str, Any]) -> list[int] | None:
    """None = leave the calendar's default reminders; [] = no reminders at all."""
    raw = args.get("reminder_minutes")
    if raw is None:
        return None
    if not isinstance(raw, list):
        raw = [raw]
    minutes = []
    for value in raw:
        try:
            m = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("'reminder_minutes' must be a list of whole minutes") from exc
        if not 0 <= m <= _MAX_REMINDER_MINUTES:
            raise ValueError("each reminder must be between 0 and 40320 minutes (4 weeks)")
        minutes.append(m)
    return sorted(set(minutes))[:5]  # Google allows at most 5 overrides


def _reminders_body(minutes: list[int]) -> dict[str, Any]:
    return {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": m} for m in minutes],
    }


def _emails(values: Any, field: str) -> list[str]:
    emails = [str(a).strip() for a in values or [] if str(a).strip()]
    bad = [a for a in emails if "@" not in a]
    if bad:
        raise ValueError(
            f"{field} must be email addresses, got {bad} — look them up with search_memory "
            "or leave them out"
        )
    return emails


def _recurrence(args: dict[str, Any]) -> tuple[bool, str | None]:
    """(given, value) — value None means 'no repeat'."""
    if "recurrence" not in args:
        return False, None
    value = args.get("recurrence")
    if value in (None, "", "none"):
        return True, None
    if value not in _RRULES:
        raise ValueError(f"'recurrence' must be one of {', '.join(RECURRENCES)} or 'none'")
    return True, str(value)


def _times(args: dict[str, Any]) -> tuple[bool, date | datetime, date | datetime]:
    start_raw = str(args["start"])
    # A date with no time can only be all-day; a time can be all-day only if asked for.
    all_day = bool(args.get("all_day")) or "T" not in start_raw
    start = _parse_local(start_raw, "start", all_day)
    end_raw = args.get("end")
    if end_raw:
        end = _parse_local(str(end_raw), "end", all_day)
        if all_day:
            # The model gives the last day ("12 to 14 Oct" → end 14th); Google wants the
            # day after it, since its all-day end date is exclusive.
            if end < start:  # type: ignore[operator]
                raise ValueError("'end' must not be before 'start'")
            end += timedelta(days=1)
        elif end <= start:  # type: ignore[operator]
            raise ValueError("'end' must be after 'start'")
    else:
        end = start + (timedelta(days=1) if all_day else _DEFAULT_DURATION)
    return all_day, start, end


def _time_body(value: date | datetime, all_day: bool, timezone: str) -> dict[str, Any]:
    if all_day:
        return {"date": value.isoformat(), "dateTime": None, "timeZone": timezone}
    return {"dateTime": value.isoformat(), "date": None, "timeZone": timezone}


def summarize_event(event: dict[str, Any], calendar_id: str, timezone: str) -> dict[str, Any]:
    """The compact view of a Google event the model reasons over and the cards display."""
    attendees = event.get("attendees") or []
    me = next((a for a in attendees if a.get("self")), None)
    return {
        "id": event.get("id"),
        "calendar_id": calendar_id,
        "summary": event.get("summary") or "(no title)",
        "start": _local_str(event.get("start"), timezone),
        "end": _local_str(event.get("end"), timezone),
        "all_day": bool((event.get("start") or {}).get("date")),
        "location": event.get("location"),
        "description": (event.get("description") or "")[:300] or None,
        "recurring": bool(event.get("recurringEventId") or event.get("recurrence")),
        "attendees": [
            {"email": a.get("email"), "response": a.get("responseStatus")}
            for a in attendees
            if not a.get("self") and a.get("email")
        ],
        "organizer": (event.get("organizer") or {}).get("email"),
        "my_response": me.get("responseStatus") if me else None,
        "html_link": event.get("htmlLink"),
    }


# --- drafts -------------------------------------------------------------------------------


def build_event_payload(args: dict[str, Any], timezone: str) -> dict[str, Any]:
    """Validates the model's create_calendar_event arguments into the complete draft the user
    reviews (end filled in, recurrence checked). Raises ValueError with a model-readable reason.
    """
    summary = str(args.get("summary") or "").strip()
    if not summary:
        raise ValueError("'summary' (the event title) is required")
    if not args.get("start"):
        raise ValueError("'start' is required")

    all_day, start, end = _times(args)
    _, recurrence = _recurrence(args)
    return {
        "calendar_id": str(args.get("calendar_id") or "primary"),
        "calendar_name": args.get("calendar_name"),
        "summary": summary,
        "description": (args.get("description") or "").strip() or None,
        "location": (args.get("location") or "").strip() or None,
        "all_day": all_day,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "timezone": timezone,
        "recurrence": recurrence,
        "attendees": _emails(args.get("attendees"), "attendees"),
        "reminder_minutes": _reminder_minutes(args),
    }


def _fmt(value: date | datetime, all_day: bool) -> str:
    """Same shape as _local_str: 'YYYY-MM-DD' or local 'YYYY-MM-DDTHH:MM'."""
    return value.isoformat() if all_day else value.strftime("%Y-%m-%dT%H:%M")


def _with_time_of(value: str, reference: str | None) -> str:
    """'2026-10-02' + reference '2026-09-25T16:00' → '2026-10-02T16:00' (keep the old time
    when a timed event is moved to another day)."""
    if "T" in value or not reference or "T" not in reference:
        return value
    return f"{value[:10]}T{reference.split('T', 1)[1]}"


def build_update_payload(
    event: dict[str, Any],
    master: dict[str, Any] | None,
    args: dict[str, Any],
    calendar_id: str,
    timezone: str,
) -> dict[str, Any]:
    """`event` is the occurrence the model picked; `master` its recurring series (given only
    for apply_to='series'). The draft carries a before/after view plus the exact PATCH body.
    All-day ends in before/after are Google's exclusive end date, like every other payload.
    """
    target = master if master is not None else event
    before = summarize_event(target, calendar_id, timezone)
    after = dict(before)
    patch: dict[str, Any] = {}

    for key in ("summary", "location", "description"):
        if args.get(key) is not None:
            value = str(args[key]).strip()
            patch[key] = value
            after[key] = value or None

    if args.get("start") or args.get("end"):
        was_all_day = before["all_day"]
        wants_all_day = args.get("all_day")
        start_raw = str(args.get("start") or before["start"])
        end_raw = args.get("end")
        if not was_all_day and wants_all_day is not True:
            # "Move the dentist to Friday" keeps its time of day.
            start_raw = _with_time_of(start_raw, before["start"])
            if end_raw:
                end_raw = _with_time_of(str(end_raw), before["end"])
        if wants_all_day is None:
            wants_all_day = "T" not in start_raw
        elif not args.get("start"):
            start_raw = start_raw[:10]
        all_day, start, end = _times({"start": start_raw, "end": end_raw, "all_day": wants_all_day})
        if not end_raw and before["start"] and before["end"] and all_day == was_all_day:
            # Moving without a new end keeps the event's original length.
            length = _parse_local(before["end"], "end", was_all_day) - _parse_local(  # type: ignore[operator]
                before["start"], "start", was_all_day
            )
            end = start + length
        if master is not None:
            # A series edit changes only the time of day of every occurrence — moving a whole
            # series to other days would fight its repeat rule.
            if all_day or was_all_day:
                raise ValueError(
                    "a whole recurring series can only have its time of day changed — to move "
                    "it to other days or make it all-day, change occurrences one at a time"
                )
            assert isinstance(start, datetime) and isinstance(end, datetime)
            series_day = datetime.fromisoformat(str(before["start"])).date()
            length = end - start
            start = datetime.combine(series_day, start.time())
            end = start + length
        patch["start"] = _time_body(start, all_day, timezone)
        patch["end"] = _time_body(end, all_day, timezone)
        after.update(all_day=all_day, start=_fmt(start, all_day), end=_fmt(end, all_day))

    given, recurrence = _recurrence(args)
    if given:
        if event.get("recurringEventId") and master is None:
            raise ValueError("to change how a recurring event repeats, use apply_to='series'")
        patch["recurrence"] = [_RRULES[recurrence]] if recurrence else []
        after["recurrence"] = recurrence

    reminders = _reminder_minutes(args)
    if reminders is not None:
        patch["reminders"] = _reminders_body(reminders)
        after["reminder_minutes"] = reminders

    add = _emails(args.get("add_attendees"), "add_attendees")
    remove = {e.lower() for e in _emails(args.get("remove_attendees"), "remove_attendees")}
    if add or remove:
        current = [a for a in target.get("attendees") or [] if a.get("email")]
        known = {a["email"].lower() for a in current}
        attendees = [a for a in current if a["email"].lower() not in remove]
        attendees += [{"email": e} for e in add if e.lower() not in known]
        patch["attendees"] = attendees
        after["attendees"] = [
            {"email": a["email"], "response": a.get("responseStatus")}
            for a in attendees
            if not a.get("self")
        ]

    if not patch:
        raise ValueError("no changes were given — pass only the fields that should change")

    guests = patch.get("attendees", target.get("attendees") or [])
    return {
        "calendar_id": calendar_id,
        "event_id": target["id"],
        "apply_to": "series" if master is not None else "this",
        "timezone": timezone,
        "before": before,
        "after": after,
        "patch": patch,
        "send_updates": "all" if any(not a.get("self") for a in guests) else "none",
    }


def build_delete_payload(
    event: dict[str, Any], master: dict[str, Any] | None, calendar_id: str, timezone: str
) -> dict[str, Any]:
    target = master if master is not None else event
    summary = summarize_event(target if master is None else event, calendar_id, timezone)
    has_guests = any(not a.get("self") for a in target.get("attendees") or [])
    organizer_is_me = (target.get("organizer") or {}).get("self", True)
    return {
        "calendar_id": calendar_id,
        "event_id": target["id"],
        "apply_to": "series" if master is not None else "this",
        "timezone": timezone,
        "event": summary,
        # Only the organizer's delete cancels it for guests; notify them when that's us.
        "send_updates": "all" if has_guests and organizer_is_me else "none",
    }


def build_rsvp_payload(
    event: dict[str, Any],
    master: dict[str, Any] | None,
    response: str,
    calendar_id: str,
    timezone: str,
) -> dict[str, Any]:
    if response not in RSVP_RESPONSES:
        raise ValueError(f"'response' must be one of {', '.join(RSVP_RESPONSES)}")
    target = master if master is not None else event
    if not any(a.get("self") for a in target.get("attendees") or []):
        raise ValueError(
            "the user isn't an invited guest of this event, so there's no invite to answer"
        )
    return {
        "calendar_id": calendar_id,
        "event_id": target["id"],
        "apply_to": "series" if master is not None else "this",
        "timezone": timezone,
        "response": response,
        "event": summarize_event(event, calendar_id, timezone),
    }


def resolve_apply_to(args: dict[str, Any], event: dict[str, Any]) -> str:
    """'series' only when asked for AND the picked event is an occurrence of one."""
    apply_to = args.get("apply_to") or "this"
    if apply_to not in APPLY_TO:
        raise ValueError("'apply_to' must be 'this' or 'series'")
    return "series" if apply_to == "series" and event.get("recurringEventId") else "this"


# --- execute (confirm endpoint only) ------------------------------------------------------


def _create_body(payload: dict[str, Any]) -> dict[str, Any]:
    key = "date" if payload["all_day"] else "dateTime"
    body: dict[str, Any] = {
        "summary": payload["summary"],
        "start": {key: payload["start"], "timeZone": payload["timezone"]},
        "end": {key: payload["end"], "timeZone": payload["timezone"]},
    }
    if payload.get("description"):
        body["description"] = payload["description"]
    if payload.get("location"):
        body["location"] = payload["location"]
    if payload.get("recurrence"):
        body["recurrence"] = [_RRULES[payload["recurrence"]]]
    if payload.get("attendees"):
        body["attendees"] = [{"email": email} for email in payload["attendees"]]
    if payload.get("reminder_minutes") is not None:
        body["reminders"] = _reminders_body(payload["reminder_minutes"])
    return body


async def create_event(access_token: str, payload: dict[str, Any]) -> dict[str, object]:
    params = {"sendUpdates": "all" if payload.get("attendees") else "none"}
    event = await _request(
        "POST",
        _events_url(payload.get("calendar_id") or "primary"),
        access_token,
        params=params,
        json=_create_body(payload),
    )
    return {"id": event.get("id"), "html_link": event.get("htmlLink")}


async def update_event(access_token: str, payload: dict[str, Any]) -> dict[str, object]:
    event = await _request(
        "PATCH",
        _events_url(payload["calendar_id"], payload["event_id"]),
        access_token,
        params={"sendUpdates": payload.get("send_updates", "none")},
        json=payload["patch"],
    )
    return {"id": event.get("id"), "html_link": event.get("htmlLink")}


async def delete_event(access_token: str, payload: dict[str, Any]) -> dict[str, object]:
    await _request(
        "DELETE",
        _events_url(payload["calendar_id"], payload["event_id"]),
        access_token,
        params={"sendUpdates": payload.get("send_updates", "none")},
    )
    return {"id": payload["event_id"], "deleted": True}


async def respond_to_invite(access_token: str, payload: dict[str, Any]) -> dict[str, object]:
    # Re-read right before writing: attendees is replaced wholesale by PATCH, so it must be
    # the current list, not the one captured when the draft was made.
    event = await get_event(
        access_token, payload["calendar_id"], payload["event_id"], payload["timezone"]
    )
    attendees = event.get("attendees") or []
    if not any(a.get("self") for a in attendees):
        raise RuntimeError("You're no longer on this event's guest list")
    for attendee in attendees:
        if attendee.get("self"):
            attendee["responseStatus"] = payload["response"]
    updated = await _request(
        "PATCH",
        _events_url(payload["calendar_id"], payload["event_id"]),
        access_token,
        params={"sendUpdates": "all"},
        json={"attendees": attendees},
    )
    return {"id": updated.get("id"), "html_link": updated.get("htmlLink")}


_EXECUTORS = {
    "create_calendar_event": create_event,
    "update_calendar_event": update_event,
    "delete_calendar_event": delete_event,
    "respond_to_invite": respond_to_invite,
}


async def execute(tool_name: str, access_token: str, payload: dict[str, Any]) -> dict[str, object]:
    return await _EXECUTORS[tool_name](access_token, payload)
