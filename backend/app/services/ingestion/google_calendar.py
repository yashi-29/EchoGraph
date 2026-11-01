from datetime import UTC, datetime, timedelta

import httpx

from app.services.ingestion.base import NormalizedItem, parse_event_start

_EVENTS_ENDPOINT = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
# The indexed window: recent past (so "what did I do last month" has memories to search) plus
# the coming year. The chat's live list_calendar_events tool covers anything outside it.
_LOOKBACK = timedelta(days=180)
_LOOKAHEAD = timedelta(days=365)
_PAGE_SIZE = 250
_MAX_EVENTS = 1000


def normalize_event(event: dict) -> NormalizedItem:
    summary = event.get("summary", "(no title)")
    description = event.get("description", "")
    attendees = [a.get("email") for a in event.get("attendees", []) if a.get("email")]
    start = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
    starts_at, all_day = parse_event_start(start)
    return NormalizedItem(
        external_id=event["id"],
        content=f"{summary}\n\n{description}".strip(),
        item_metadata={
            "start": start,
            "end": event.get("end", {}).get("dateTime") or event.get("end", {}).get("date"),
            "all_day": all_day,
            "location": event.get("location"),
            "attendees": attendees,
            "html_link": event.get("htmlLink"),
        },
        occurred_at=starts_at,
    )


async def fetch_and_normalize(access_token: str) -> list[NormalizedItem]:
    headers = {"Authorization": f"Bearer {access_token}"}
    now = datetime.now(UTC)
    params: dict[str, str | int] = {
        "timeMin": (now - _LOOKBACK).isoformat(),
        "timeMax": (now + _LOOKAHEAD).isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": _PAGE_SIZE,
    }
    events: list[dict] = []
    async with httpx.AsyncClient() as client:
        while len(events) < _MAX_EVENTS:
            response = await client.get(_EVENTS_ENDPOINT, headers=headers, params=params)
            response.raise_for_status()
            page = response.json()
            events.extend(page.get("items", []))
            if not page.get("nextPageToken"):
                break
            params["pageToken"] = page["nextPageToken"]
    return [normalize_event(event) for event in events[:_MAX_EVENTS]]
