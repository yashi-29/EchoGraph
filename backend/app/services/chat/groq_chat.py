import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from groq import AsyncGroq
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.memory_item import MemoryItem
from app.models.pending_action import PendingAction
from app.services.actions import email_drafts
from app.services.chat import calendar_tools
from app.services.chat.retrieval import search_memories

settings = get_settings()

_client: AsyncGroq | None = None
# Room for find → act chains (list events, then update/delete) and multi-part requests.
_MAX_TOOL_ROUNDS = 8

_SYSTEM_PROMPT = (
    "You are the user's personal digital twin: you know them through their own indexed "
    "memories (emails, calendar events, uploaded documents and images, voice notes, bookmarks) "
    "and act for "
    "them in the apps they've connected. No memories are in context yet — call search_memory "
    "before answering anything about the user's data: order='recent' with a source for 'my "
    "last N emails', order='relevance' with a query for topics. If nothing relevant turns up, "
    "say so — never invent information.\n\n"
    "Understand what the user means even from casual, misspelled or shorthand wording ('add "
    "nehas bday 28 sept', 'mail aditya the NOC'). When they ask for something in a connected "
    "app, call its tool straight away — never say you can't, never explain how to do it "
    "themselves, never ask 'shall I?': action tools only create a draft the user confirms with "
    "a button, so the draft IS the confirmation. If a detail is ambiguous, pick the likeliest "
    "reading and draft it; ask only when something essential is missing entirely. Handle "
    "every request in a message (several tools if needed). Never claim an action is done — "
    "after drafting, say in one sentence what you prepared, e.g. \"I've drafted Neha's "
    'birthday for 28 September, repeating yearly — confirm below."\n\n'
    "search_memory results are already shown to the user as a list below your reply — never "
    "repeat them as a list or restate their subjects/dates. Answer in 1-3 sentences: for a "
    "browse request one intro line ('Here are your 10 most recent emails.'); for a specific "
    "question answer it and cite items inline like [1] or [1][3]."
)

_EMAIL_RULES = (
    " Email rules for draft_email: 'to' must be a real address. If the user names a person "
    "without one, search_memory for their emails to find it; ask only if nothing turns up. "
    "Write the subject and body yourself from what the user asked, in their voice. Never "
    "leave placeholders like [Your Name] or [Date] — fill them in, or leave that part out. "
    "To send a file ('mail aditya the NOC'), search_memory for it first and pass the id of "
    "the matching document or email attachment in attachment_ids — never paste its text "
    "into the body instead. To reply to an email, search_memory for it and pass its id as "
    "reply_to_id so the reply stays in the same thread."
)

# Tools that use a connected app, keyed to the connector provider that must be present.
_APP_NAMES = {"gmail": "Gmail", "google_calendar": "Google Calendar"}
_TOOL_PROVIDER = {
    "draft_email": "gmail",
    **{name: "google_calendar" for name in calendar_tools.ALL_TOOLS},
}
# The subset with a real side effect: these only ever produce a PendingAction draft.
_DRAFT_TOOLS = {"draft_email", *calendar_tools.WRITE_TOOLS}

_SEARCH_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "search_memory",
        "description": (
            "Search the user's indexed memories. Call this before answering any question "
            "that needs the user's own data — never guess or answer from context alone."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": ["string", "null"],
                    "description": "Semantic search text — required when order='relevance'",
                },
                "source": {
                    # Nullable: models often send `"source": null` for "all sources", and
                    # Groq rejects the whole tool call if the schema doesn't allow it.
                    "type": ["string", "null"],
                    "enum": [
                        "gmail",
                        "google_calendar",
                        "document",
                        "image",
                        "voice_note",
                        "bookmark",
                        None,
                    ],
                    "description": (
                        "Restrict to one source; omit to search all sources. 'gmail' = emails "
                        "and their attachments, 'document' = files the user uploaded (PDFs, "
                        "Word, notes), 'image' = uploaded photos, screenshots and scans "
                        "(text read by OCR), 'voice_note' = transcribed "
                        "recordings, 'bookmark' = saved browser bookmarks"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 10)",
                },
                "order": {
                    "type": "string",
                    "enum": ["relevance", "recent"],
                    "description": (
                        "'relevance' = semantic similarity to query; "
                        "'recent' = most recently indexed first"
                    ),
                },
            },
            "required": ["order"],
        },
    },
}

_DRAFT_EMAIL_TOOL = {
    "type": "function",
    "function": {
        "name": "draft_email",
        "description": (
            "Draft an email on the user's behalf in Gmail. This never sends anything — it only "
            "creates a draft that the user must explicitly review and confirm before it is "
            "sent. Use this whenever the user asks you to email, mail, write to, or reply to "
            "someone, including sending them a file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": ["string", "null"],
                    "description": (
                        "Recipient email address(es), comma-separated. May be omitted when "
                        "replying — the reply then goes to the original sender."
                    ),
                },
                "cc": {
                    "type": ["string", "null"],
                    "description": "Cc address(es), comma-separated",
                },
                "subject": {
                    "type": ["string", "null"],
                    "description": "May be omitted when replying ('Re: <original>' is used)",
                },
                "body": {"type": "string", "description": "Plain-text email body"},
                "reply_to_id": {
                    "type": ["string", "null"],
                    "description": (
                        "To reply to an email (in the same thread), its id from search_memory "
                        "results"
                    ),
                },
                "attachment_ids": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": (
                        "Files to attach: ids from search_memory results of uploaded documents "
                        "or images (source 'document' or 'image') or email attachments "
                        "(source 'gmail')"
                    ),
                },
            },
            "required": ["body"],
        },
    },
}

_APP_TOOLS = {"draft_email": _DRAFT_EMAIL_TOOL, **calendar_tools.TOOL_SCHEMAS}


_TITLE_PROMPT = (
    "Write a short title (at most 6 words) for a conversation that opens with the user "
    "message below. Reply with the title only — no quotes, no trailing punctuation."
)
_TITLE_MAX_CHARS = 80
# Per search result: a whole document chunk (~2000 chars) plus its title line.
_CONTEXT_ITEM_CHARS = 2400


def _get_client() -> AsyncGroq:
    global _client
    if _client is None:
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is not configured")
        # Groq's free tier is rate limited per minute; a find-then-act turn can brush the cap,
        # so retry (the SDK waits out the server's retry-after) before giving up.
        _client = AsyncGroq(api_key=settings.groq_api_key, max_retries=4)
    return _client


@dataclass(frozen=True)
class ChatPreferences:
    """The user's Settings → Twin behavior / Privacy choices, applied to every chat turn."""

    allow_content: bool = True
    tone: str = "balanced"
    memory_window_days: int | None = None


@dataclass(frozen=True)
class TurnContext:
    """Per-request facts the model needs to act: where "today" is, and which apps it can use."""

    timezone: str = "UTC"
    connected_providers: frozenset[str] = field(default_factory=frozenset)
    user_name: str | None = None


def resolve_timezone(name: str | None) -> str:
    """The browser-reported IANA zone if Python knows it, else UTC."""
    if not name:
        return "UTC"
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"
    return name


_TONE_INSTRUCTIONS = {
    "balanced": "",
    "concise": (
        " Style: be direct and concise. Lead with the answer, keep sentences short, skip "
        "pleasantries, and use bullet points for anything list-like."
    ),
    "warm": (
        " Style: be warm and reflective. Write conversationally and with empathy, and where it "
        "helps, gently point out patterns across the memories — but never invent facts."
    ),
}
_CONTENT_OFF_INSTRUCTION = (
    " The user has disabled message-content access: search results show only each item's "
    "title, source and date. Answer from those alone and say so if the question needs more."
)


def _available_app_tools(context: TurnContext) -> list[str]:
    return [
        name for name, provider in _TOOL_PROVIDER.items() if provider in context.connected_providers
    ]


def _context_instructions(context: TurnContext) -> str:
    now = datetime.now(ZoneInfo(context.timezone))
    text = (
        f"\n\nRight now it is {now:%A, %d %B %Y, %H:%M} in the user's timezone "
        f"({context.timezone}). Tomorrow is {now + timedelta(days=1):%A, %d %B}. If nothing "
        "matches the day the user named, say so rather than acting on a different day."
    )
    if context.user_name:
        text += f" The user's name is {context.user_name}."
    connected = [_APP_NAMES[p] for p in _APP_NAMES if p in context.connected_providers]
    missing = [_APP_NAMES[p] for p in _APP_NAMES if p not in context.connected_providers]
    if connected:
        text += f" Connected apps you can act in: {', '.join(connected)}."
    if missing:
        text += (
            f" Not connected: {', '.join(missing)} — if the user asks for something that needs "
            "one of these, say in one sentence that they need to connect it on the Connectors "
            "page first. Never pretend you did it."
        )
    tools = _available_app_tools(context)
    if "create_calendar_event" in tools:
        text += calendar_tools.RULES
    if "draft_email" in tools:
        text += _EMAIL_RULES
    return text


def _system_prompt(preferences: ChatPreferences, context: TurnContext) -> str:
    prompt = _SYSTEM_PROMPT + _context_instructions(context)
    prompt += _TONE_INSTRUCTIONS.get(preferences.tone, "")
    if not preferences.allow_content:
        prompt += _CONTENT_OFF_INSTRUCTION
    return prompt


def _tools_for(context: TurnContext) -> list[dict]:
    return [_SEARCH_MEMORY_TOOL, *(_APP_TOOLS[n] for n in _available_app_tools(context))]


def _clip(content: str) -> str:
    # A document chunk fits whole; a long email body is cut so ten of them stay within
    # Groq's per-minute token budget.
    if len(content) <= _CONTEXT_ITEM_CHARS:
        return content
    return content[:_CONTEXT_ITEM_CHARS].rstrip() + "… [truncated]"


def _context_block(memories: list[MemoryItem], offset: int, allow_content: bool = True) -> str:
    if not memories:
        return "No matching memories were found."
    if not allow_content:
        # Title line + date only — the body never reaches the model.
        return "\n\n".join(
            f"[{i}] (source: {item.source}, id: {item.external_id}, "
            f"date: {item.occurred_at:%Y-%m-%d})\n{item.content.partition(chr(10))[0]}"
            for i, item in enumerate(memories, start=offset + 1)
        )
    return "\n\n".join(
        f"[{i}] (source: {item.source}, id: {item.external_id})\n{_clip(item.content)}"
        for i, item in enumerate(memories, start=offset + 1)
    )


def _tool_error(message: str) -> str:
    return json.dumps({"status": "error", "message": message})


async def _draft_payload(
    name: str,
    args: dict,
    calendar: calendar_tools.CalendarSession,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> dict:
    """The draft the user will review, or ValueError telling the model what to fix."""
    if name in calendar_tools.WRITE_TOOLS:
        return await calendar_tools.prepare_draft(name, args, calendar)
    return await email_drafts.prepare_draft(args, user_id, db)


async def _run_tool_call(
    tool_call: object,
    user_id: uuid.UUID,
    db: AsyncSession,
    memories_so_far: int,
    preferences: ChatPreferences,
    context: TurnContext,
    calendar: calendar_tools.CalendarSession,
) -> tuple[str, list[MemoryItem], PendingAction | None]:
    name = tool_call.function.name  # type: ignore[attr-defined]
    try:
        args = json.loads(tool_call.function.arguments or "{}")  # type: ignore[attr-defined]
    except json.JSONDecodeError:
        return _tool_error("Arguments were not valid JSON — try the call again."), [], None

    if name == "search_memory":
        args.pop("since", None)  # the memory window is the user's setting, not the model's
        # An explicit null means "not given" — fall back to search_memories' defaults.
        args = {key: value for key, value in args.items() if value is not None}
        since = (
            datetime.now(UTC) - timedelta(days=preferences.memory_window_days)
            if preferences.memory_window_days
            else None
        )
        memories = await search_memories(user_id, db, **args, since=since)
        return (
            _context_block(memories, memories_so_far, preferences.allow_content),
            memories,
            None,
        )

    if name not in _TOOL_PROVIDER:
        return _tool_error(f"Unknown tool: {name}"), [], None
    if name not in _available_app_tools(context):
        app = _APP_NAMES[_TOOL_PROVIDER[name]]
        return _tool_error(f"{app} isn't connected — tell the user to connect it."), [], None
    # Optional fields the model sent as null mean "not given" (not "clear it").
    args = {key: value for key, value in args.items() if value is not None}

    if name not in _DRAFT_TOOLS:
        return await calendar_tools.run_read_tool(name, args, calendar), [], None

    try:
        payload = await _draft_payload(name, args, calendar, user_id, db)
    except ValueError as exc:
        return _tool_error(str(exc)), [], None
    pending_action = PendingAction(
        user_id=user_id, tool_name=name, payload=payload, status="pending"
    )
    db.add(pending_action)
    await db.commit()
    await db.refresh(pending_action)
    note = "Not executed yet — the user must confirm the draft card."
    if name in calendar_tools.WRITE_TOOLS and not await calendar.can_write():
        note += (
            " Google Calendar is connected read-only: tell the user to reconnect it on the "
            "Connectors page (granting edit access) before confirming."
        )
    result = json.dumps(
        {
            "status": "draft_created",
            "action_id": str(pending_action.id),
            "draft": payload,
            "note": note,
        }
    )
    return result, [], pending_action


async def generate_reply(
    message: str,
    history: list[dict[str, str]],
    user_id: uuid.UUID,
    db: AsyncSession,
    preferences: ChatPreferences | None = None,
    context: TurnContext | None = None,
) -> tuple[str, list[MemoryItem], list[PendingAction]]:
    client = _get_client()
    preferences = preferences or ChatPreferences()
    context = context or TurnContext()
    tools = _tools_for(context)
    calendar = calendar_tools.CalendarSession(user_id, db, context.timezone)
    messages: list[dict] = [
        {"role": "system", "content": _system_prompt(preferences, context)},
        *history,
        {"role": "user", "content": message},
    ]
    all_memories: list[MemoryItem] = []
    pending_actions: list[PendingAction] = []

    for _ in range(_MAX_TOOL_ROUNDS):
        response = await client.chat.completions.create(
            model=settings.groq_chat_model,
            messages=messages,  # type: ignore[arg-type]
            tools=tools,  # type: ignore[arg-type]
            temperature=0.3,
        )
        choice_message = response.choices[0].message

        if not choice_message.tool_calls:
            return choice_message.content or "", all_memories, pending_actions

        messages.append(
            {
                "role": "assistant",
                "content": choice_message.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in choice_message.tool_calls
                ],
            }
        )
        for tool_call in choice_message.tool_calls:
            tool_result, memories, action = await _run_tool_call(
                tool_call, user_id, db, len(all_memories), preferences, context, calendar
            )
            all_memories.extend(memories)
            if action is not None:
                pending_actions.append(action)
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": tool_result})

    # Ran out of tool-call rounds — force a final answer with whatever's gathered so far.
    final = await client.chat.completions.create(
        model=settings.groq_chat_model,
        messages=messages,  # type: ignore[arg-type]
        temperature=0.3,
    )
    return final.choices[0].message.content or "", all_memories, pending_actions


def fallback_title(message: str) -> str:
    words = message.split()
    title = " ".join(words[:8])
    if len(words) > 8 or len(title) > 60:
        title = title[:60].rstrip() + "…"
    return title or "New conversation"


async def generate_title(message: str) -> str:
    """A short sidebar title for a new conversation. Cosmetic, so any failure (Groq down,
    empty or odd output) falls back to the message's opening words instead of failing the turn.
    """
    try:
        response = await _get_client().chat.completions.create(
            model=settings.groq_chat_model,
            messages=[
                {"role": "system", "content": _TITLE_PROMPT},
                {"role": "user", "content": message[:2000]},
            ],
            temperature=0.2,
        )
        raw = response.choices[0].message.content or ""
    except Exception:
        return fallback_title(message)
    lines = raw.strip().splitlines()
    title = lines[0].strip().strip("\"'*#").rstrip(".").strip() if lines else ""
    return title[:_TITLE_MAX_CHARS] if title else fallback_title(message)
