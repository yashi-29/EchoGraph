"""Reflections: short, grounded observations about patterns in the user's own memories.

Pipeline (generate_reflections):
1. Gather a window of memories (last 30 days, widened to the most recent 90 items if sparse)
   plus the next 14 days of calendar events.
2. Compute FACTS deterministically — per-week counts, calendar load by weekday/hour in the
   user's timezone, top senders, upcoming vs. previous fortnight — so numeric claims rest on
   arithmetic, not on the model counting.
3. Ask the LLM for a few reflections as JSON, each citing memory numbers as evidence.
4. Keep only well-formed reflections that cite at least one real memory, snapshot that
   evidence, and store them as one batch.

Respects Settings → "Allow twin to reference message content": when off, the model only sees
titles, senders and dates.
"""

import html
import json
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from groq import AsyncGroq
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.memory_item import MemoryItem, is_first_chunk
from app.models.reflection import CATEGORIES, TONES, Reflection
from app.models.user import User
from app.services.graph.entities import classify_address

settings = get_settings()

WINDOW = timedelta(days=30)
LOOKAHEAD = timedelta(days=14)
MIN_MEMORIES = 8  # below this there's nothing meaningful to reflect on
MAX_ITEMS = 90
GENERATE_COOLDOWN = timedelta(minutes=10)
WEEKLY = timedelta(days=7)
_RECENT_HEADLINES = 12
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class NotEnoughMemories(Exception):
    pass


class GenerationFailed(Exception):
    pass


@dataclass
class _Item:
    memory: MemoryItem
    title: str
    snippet: str
    local: datetime
    sender: str | None


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


# Zero-width/invisible characters newsletters pad their preview text with.
_INVISIBLE = re.compile("[\u200b-\u200f\u034f\u00ad\u2060\ufeff]")
# Trailing "…, indicating/suggesting X." clauses: the model guessing at motives despite the
# prompt. Cut deterministically so observations stay descriptive.
_SPECULATION = re.compile(
    r",?\s*(?:—\s*)?\b(?:indicating|suggesting|showing|signall?ing|reflecting|implying|"
    r"which (?:suggests|indicates|shows|reflects))\b[^.!?]*",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    return " ".join(_INVISIBLE.sub("", html.unescape(text)).split())


def trim_speculation(text: str) -> str:
    trimmed = _SPECULATION.sub("", text).strip()
    trimmed = re.sub(r"\s+([.!?])", r"\1", trimmed)
    return trimmed if len(trimmed) >= 20 else text


def _split(content: str) -> tuple[str, str]:
    title, _, body = content.partition("\n\n")
    title = _clean(title)[:140] or "(untitled)"
    return title, _clean(body)


def _sender(memory: MemoryItem, self_addresses: set[str]) -> str | None:
    if memory.source != "gmail":
        return None
    entity = classify_address(str((memory.item_metadata or {}).get("from") or ""), self_addresses)
    return entity.name if entity else None


def compute_facts(items: list[_Item], upcoming: list[_Item], prior_fortnight: int) -> list[str]:
    """Plain-English, already-counted facts the model may quote."""
    facts: list[str] = []
    if not items:
        return facts
    start, end = items[-1].local, items[0].local
    facts.append(
        f"Window: {start:%b %d} – {end:%b %d} ({len(items)} memories: "
        + ", ".join(f"{n} {s}" for s, n in Counter(i.memory.source for i in items).most_common())
        + ")."
    )

    weeks: Counter = Counter()
    for i in items:
        weeks[(i.local - timedelta(days=i.local.weekday())).date()] += 1  # keyed by the Monday
    if len(weeks) > 1:
        facts.append(
            "Memories per week: "
            + ", ".join(f"week of {monday:%b %d}: {n}" for monday, n in sorted(weeks.items()))
            + "."
        )

    events = [i for i in items if i.memory.source == "google_calendar"]
    if events:
        by_day = Counter(i.local.weekday() for i in events)
        facts.append(
            "Calendar events by weekday: "
            + ", ".join(f"{_WEEKDAYS[d]} {by_day.get(d, 0)}" for d in range(7))
            + "."
        )
        timed = [i for i in events if not (i.memory.item_metadata or {}).get("all_day")]
        early = sum(1 for i in timed if i.local.hour < 9)
        late = sum(1 for i in timed if i.local.hour >= 18)
        if timed:
            facts.append(
                f"Of {len(timed)} timed events, {early} started before 9:00 and {late} at or "
                "after 18:00 (user's local time)."
            )
    facts.append(
        f"Calendar: {len(upcoming)} events in the next 14 days vs {prior_fortnight} in the "
        "previous 14 days."
    )

    senders = Counter(i.sender for i in items if i.sender)
    if senders:
        facts.append(
            "Most frequent email senders: "
            + ", ".join(f"{name} ({n})" for name, n in senders.most_common(5))
            + "."
        )
    return facts


_PROMPT = """You write reflections for someone's personal "digital twin": short, observant, \
kind notes about patterns in their own emails, calendar events and files.

You get FACTS (already counted — trust these numbers) and numbered MEMORIES.
Write {count} reflections. Rules:
- Each describes a pattern, trend or notable change, and cites the memory numbers that \
support it in "evidence" (at least one, ideally several).
- Only say what the data shows. Never invent feelings, intentions, people, numbers or events. \
Don't guess at health or mental state. Receiving mail is not an interest or a goal — only \
treat something as the user's focus if they acted on it (sent, replied, scheduled, uploaded).
- Describe what happened, not why. No "suggesting", "indicating", "showing a preference" or \
other guesses about motives.
- Second person, warm and plain. "observation": 1–2 sentences, at most 45 words. \
"headline": 3–7 words.
- "category": one of {categories}.
- "tone": "positive" (something going well), "neutral" (simply noticing) or "watch" \
(worth keeping an eye on).
- "tags": 1–2 short labels, e.g. "Busy mornings", "Newsletters".
- Prefer variety across categories. Skip trivial observations like "you received emails".
{avoid}
Reply with JSON only, exactly:
{{"reflections": [{{"category": "...", "tone": "...", "headline": "...", \
"observation": "...", "tags": ["..."], "evidence": [1, 2]}}]}}"""


def _memory_lines(items: list[_Item], allow_content: bool) -> list[str]:
    lines = []
    for n, i in enumerate(items, start=1):
        parts = [f"[{n}] {i.local:%Y-%m-%d %a %H:%M}", i.memory.source]
        if i.sender:
            parts.append(f"from {i.sender}")
        line = " · ".join(parts) + f" · {i.title}"
        if allow_content and i.snippet:
            line += f" — {i.snippet[:160]}"
        lines.append(line)
    return lines


def parse_reflections(raw: str, item_count: int) -> list[dict]:
    """Validated reflection dicts from the model's reply; malformed or uncited ones dropped."""
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for r in data.get("reflections", []) if isinstance(data, dict) else []:
        if not isinstance(r, dict):
            continue
        observation = trim_speculation(str(r.get("observation") or "").strip())
        headline = str(r.get("headline") or "").strip().rstrip(".")
        evidence = sorted(
            {
                int(e)
                for e in (r.get("evidence") or [])
                if isinstance(e, int | str) and str(e).isdigit() and 1 <= int(e) <= item_count
            }
        )
        if not observation or not headline or not evidence:
            continue
        category = str(r.get("category") or "").lower()
        tone = str(r.get("tone") or "").lower()
        tags = [str(t).strip()[:30] for t in (r.get("tags") or []) if str(t).strip()][:2]
        out.append(
            {
                "category": category if category in CATEGORIES else "personal",
                "tone": tone if tone in TONES else "neutral",
                "headline": headline[:160],
                "body": observation[:600],
                "tags": tags,
                "evidence": evidence,
            }
        )
    return out


async def _llm(prompt: str, memories: str) -> str:
    if not settings.groq_api_key:
        raise GenerationFailed("GROQ_API_KEY is not configured")
    try:
        response = await AsyncGroq(api_key=settings.groq_api_key).chat.completions.create(
            model=settings.groq_chat_model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": memories},
            ],
            temperature=0.4,
        )
    except Exception as exc:
        raise GenerationFailed(str(exc)) from exc
    return response.choices[0].message.content or ""


async def latest_batch_at(user_id: uuid.UUID, db: AsyncSession) -> datetime | None:
    return await db.scalar(
        select(func.max(Reflection.created_at)).where(Reflection.user_id == user_id)
    )


async def generate_reflections(
    user: User, db: AsyncSession, self_addresses: set[str] | None = None
) -> list[Reflection]:
    """Generates and stores one batch for `user`. Raises NotEnoughMemories / GenerationFailed."""
    now = datetime.now(UTC)
    zone = _zone(user.timezone)
    self_addresses = self_addresses or {user.email.lower()}
    base = [MemoryItem.user_id == user.id, is_first_chunk()]

    window = list(
        (
            await db.execute(
                select(MemoryItem)
                .where(*base, MemoryItem.occurred_at <= now, MemoryItem.occurred_at >= now - WINDOW)
                .order_by(MemoryItem.occurred_at.desc())
                .limit(MAX_ITEMS)
            )
        ).scalars()
    )
    if len(window) < MIN_MEMORIES:  # sparse month → fall back to the most recent memories
        window = list(
            (
                await db.execute(
                    select(MemoryItem)
                    .where(*base, MemoryItem.occurred_at <= now)
                    .order_by(MemoryItem.occurred_at.desc())
                    .limit(MAX_ITEMS)
                )
            ).scalars()
        )
    if len(window) < MIN_MEMORIES:
        raise NotEnoughMemories

    def wrap(m: MemoryItem) -> _Item:
        title, body = _split(m.content)
        return _Item(m, title, body, m.occurred_at.astimezone(zone), _sender(m, self_addresses))

    items = [wrap(m) for m in window]
    calendar = [MemoryItem.user_id == user.id, MemoryItem.source == "google_calendar"]
    upcoming = [
        wrap(m)
        for m in (
            await db.execute(
                select(MemoryItem).where(
                    *calendar,
                    MemoryItem.occurred_at > now,
                    MemoryItem.occurred_at <= now + LOOKAHEAD,
                )
            )
        ).scalars()
    ]
    prior = await db.scalar(
        select(func.count())
        .select_from(MemoryItem)
        .where(*calendar, MemoryItem.occurred_at <= now, MemoryItem.occurred_at > now - LOOKAHEAD)
    )

    recent = (
        await db.execute(
            select(Reflection.headline)
            .where(Reflection.user_id == user.id, Reflection.created_at >= now - timedelta(days=45))
            .order_by(Reflection.created_at.desc())
            .limit(_RECENT_HEADLINES)
        )
    ).scalars()
    recent_list = list(recent)
    avoid = (
        "- Don't repeat these recent reflections: " + "; ".join(f'"{h}"' for h in recent_list)
        if recent_list
        else ""
    )
    prompt = _PROMPT.format(
        count=3 if len(items) < 30 else 4, categories=", ".join(CATEGORIES), avoid=avoid
    )
    facts = compute_facts(items, upcoming, prior or 0)
    user_message = (
        "FACTS:\n"
        + "\n".join(f"- {f}" for f in facts)
        + "\n\nMEMORIES (newest first):\n"
        + "\n".join(_memory_lines(items, user.allow_content_in_chat))
    )

    parsed = parse_reflections(await _llm(prompt, user_message), len(items))
    if not parsed:
        raise GenerationFailed("The model returned no usable reflections")

    batch_id = uuid.uuid4()
    period_start, period_end = window[-1].occurred_at, window[0].occurred_at
    created = []
    for r in parsed:
        evidence = [
            {
                "memory_id": str(items[n - 1].memory.id),
                "external_id": items[n - 1].memory.external_id,
                "source": items[n - 1].memory.source,
                "title": items[n - 1].title,
                # Shown to the user (chat citations), never sent to the model here.
                "snippet": items[n - 1].snippet[:160],
                "occurred_at": items[n - 1].memory.occurred_at.isoformat(),
            }
            for n in r.pop("evidence")
        ]
        reflection = Reflection(
            user_id=user.id,
            batch_id=batch_id,
            evidence=evidence,
            period_start=period_start,
            period_end=period_end,
            **r,
        )
        db.add(reflection)
        created.append(reflection)
    await db.commit()
    for reflection in created:
        await db.refresh(reflection)
    return created
