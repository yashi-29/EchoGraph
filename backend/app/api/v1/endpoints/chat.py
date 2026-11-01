import json
import re
import uuid
from datetime import UTC, datetime

import groq
import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.connector import Connector
from app.models.conversation import Conversation, ConversationMessage
from app.models.memory_item import MemoryItem
from app.models.pending_action import PendingAction
from app.models.user import User
from app.schemas.actions import PendingActionRead
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    Citation,
    ConversationDetail,
    ConversationSummary,
    ConversationUpdate,
    MessageRead,
)
from app.services.actions import email_drafts, gmail_send, google_calendar_events
from app.services.chat.groq_chat import (
    ChatPreferences,
    TurnContext,
    generate_reply,
    generate_title,
    resolve_timezone,
)
from app.services.oauth.tokens import ConnectorAccessExpired, get_valid_access_token

router = APIRouter()


# How many prior messages of a thread are replayed to the model each turn.
_HISTORY_LIMIT = 20
_PREVIEW_CHARS = 120
_SNIPPET_CHARS = 200


def _citation_from(item: MemoryItem) -> Citation:
    # Every normalized item's content is "title\n\nbody" (email subject+snippet,
    # calendar summary+description) — split it rather than duplicate the title in the snippet.
    first_line, _, rest = item.content.partition("\n\n")
    title = item.item_metadata.get("subject") or first_line or "(untitled)"
    snippet = rest.strip()[:_SNIPPET_CHARS] if rest.strip() else "No further details."
    return Citation(
        source=item.source,
        external_id=item.external_id,
        title=str(title),
        snippet=snippet,
        occurred_at=item.occurred_at,
    )


async def _get_owned_conversation(
    conversation_id: uuid.UUID, current_user: User, db: AsyncSession
) -> Conversation:
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != current_user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conversation


# How draft notes used to be appended to replayed replies. The model learned to imitate them
# in its own answers, so they're now sent separately and any copy is stripped from replies.
_DRAFT_NOTE = re.compile(r"\s*\[\w+ draft, status=\w+: \{.*?\}\]", re.S)


def strip_draft_notes(text: str) -> str:
    return _DRAFT_NOTE.sub("", text).strip()


def _history_messages(message: ConversationMessage) -> list[dict[str, str]]:
    content = strip_draft_notes(message.content) if message.role == "assistant" else message.content
    replayed = [{"role": message.role, "content": content}]
    # Replay what the twin drafted (and what became of it) so follow-ups like "make it 6pm
    # instead" or "did that email go out?" have something to refer to — as a separate
    # context note, so it never reads like part of the reply the model should copy.
    if message.pending_actions:
        drafts = "\n".join(
            f"- {a.tool_name} ({a.status}): {json.dumps(a.payload)}"
            for a in message.pending_actions
        )
        replayed.append(
            {
                "role": "system",
                "content": (
                    "Context for you only — the user already saw these as draft cards under "
                    "your previous reply. Never write this list, its JSON or its field names "
                    f"in a reply:\n{drafts}"
                ),
            }
        )
    return replayed


async def _load_history(conversation_id: uuid.UUID, db: AsyncSession) -> list[dict[str, str]]:
    rows = await db.execute(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation_id)
        .order_by(ConversationMessage.seq.desc())
        .limit(_HISTORY_LIMIT)
    )
    return [entry for m in reversed(rows.scalars().all()) for entry in _history_messages(m)]


def _message_read(message: ConversationMessage) -> MessageRead:
    read = MessageRead.model_validate(message)
    if message.role == "assistant":  # replies saved before draft notes were filtered out
        read.content = strip_draft_notes(read.content)
    return read


async def _connected_providers(user_id: uuid.UUID, db: AsyncSession) -> frozenset[str]:
    rows = await db.execute(select(Connector.provider).where(Connector.user_id == user_id))
    return frozenset(rows.scalars().all())


@router.post("/", response_model=ChatResponse)
async def send_message(
    payload: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    is_new = payload.conversation_id is None
    if payload.conversation_id is not None:
        conversation = await _get_owned_conversation(payload.conversation_id, current_user, db)
        history = await _load_history(conversation.id, db)
    else:
        conversation = Conversation(user_id=current_user.id, title="New conversation")
        db.add(conversation)
        await db.flush()
        history = []

    db.add(
        ConversationMessage(conversation_id=conversation.id, role="user", content=payload.message)
    )
    # Flush now so the question takes a lower `seq` than its answer.
    await db.flush()
    preferences = ChatPreferences(
        allow_content=current_user.allow_content_in_chat,
        tone=current_user.response_tone,
        memory_window_days=current_user.memory_window_days,
    )
    context = TurnContext(
        timezone=resolve_timezone(payload.timezone),
        connected_providers=await _connected_providers(current_user.id, db),
        user_name=current_user.full_name,
    )
    try:
        reply, memories, pending_actions = await generate_reply(
            payload.message, history, current_user.id, db, preferences, context
        )
    except groq.RateLimitError as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Your twin is getting a lot of requests right now — try again in a few seconds.",
        ) from exc
    # search_memory may be called more than once per turn — dedupe before citing.
    seen: set[uuid.UUID] = set()
    citations = []
    for item in memories:
        if item.id in seen:
            continue
        seen.add(item.id)
        citations.append(_citation_from(item))

    reply = strip_draft_notes(reply)
    assistant_message = ConversationMessage(
        conversation_id=conversation.id,
        role="assistant",
        content=reply,
        citations=[c.model_dump(mode="json") for c in citations],
    )
    db.add(assistant_message)
    await db.flush()
    for action in pending_actions:
        action.message_id = assistant_message.id
    if is_new:
        conversation.title = await generate_title(payload.message)
    conversation.updated_at = datetime.now(UTC)
    await db.commit()

    return ChatResponse(
        conversation_id=conversation.id,
        conversation_title=conversation.title,
        message_id=assistant_message.id,
        reply=reply,
        citations=citations,
        pending_actions=[PendingActionRead.model_validate(a) for a in pending_actions],
    )


@router.get("/conversations", response_model=list[ConversationSummary])
async def list_conversations(
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[ConversationSummary]:
    last_message = (
        select(func.left(ConversationMessage.content, _PREVIEW_CHARS))
        .where(ConversationMessage.conversation_id == Conversation.id)
        .order_by(ConversationMessage.seq.desc())
        .limit(1)
        .correlate(Conversation)
        .scalar_subquery()
    )
    rows = await db.execute(
        select(Conversation, last_message)
        .where(Conversation.user_id == current_user.id)
        .order_by(Conversation.updated_at.desc())
    )
    return [
        ConversationSummary(id=c.id, title=c.title, preview=preview, updated_at=c.updated_at)
        for c, preview in rows.all()
    ]


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConversationDetail:
    conversation = await _get_owned_conversation(conversation_id, current_user, db)
    messages = await db.execute(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation.id)
        .order_by(ConversationMessage.seq)
    )
    return ConversationDetail(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        messages=[_message_read(m) for m in messages.scalars().all()],
    )


@router.patch("/conversations/{conversation_id}", response_model=ConversationSummary)
async def rename_conversation(
    conversation_id: uuid.UUID,
    payload: ConversationUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConversationSummary:
    conversation = await _get_owned_conversation(conversation_id, current_user, db)
    conversation.title = payload.title.strip() or conversation.title
    await db.commit()
    return ConversationSummary(
        id=conversation.id,
        title=conversation.title,
        preview=None,
        updated_at=conversation.updated_at,
    )


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    conversation = await _get_owned_conversation(conversation_id, current_user, db)
    # conversation_messages rows go with it via ON DELETE CASCADE.
    await db.execute(delete(Conversation).where(Conversation.id == conversation.id))
    await db.commit()


async def _get_owned_action(
    action_id: uuid.UUID, current_user: User, db: AsyncSession
) -> PendingAction:
    action = await db.get(PendingAction, action_id)
    if action is None or action.user_id != current_user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Action not found")
    return action


async def _get_connector(provider: str, current_user: User, db: AsyncSession) -> Connector | None:
    return (
        await db.execute(
            select(Connector).where(
                Connector.user_id == current_user.id, Connector.provider == provider
            )
        )
    ).scalar_one_or_none()


async def _execute_draft_email(
    action: PendingAction, current_user: User, db: AsyncSession
) -> dict[str, object]:
    connector = await _get_connector("gmail", current_user, db)
    if connector is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Gmail isn't connected")

    access_token = await get_valid_access_token(connector, db)
    payload = action.payload
    reply = payload.get("reply_to") or {}
    attachments = await email_drafts.load_attachments(
        payload.get("attachments") or [], current_user.id, db, access_token
    )
    return await gmail_send.send_email(
        access_token,
        to=payload["to"],
        subject=payload["subject"],
        body=payload["body"],
        cc=payload.get("cc"),
        thread_id=reply.get("thread_id"),
        in_reply_to=reply.get("in_reply_to"),
        references=reply.get("references"),
        attachments=attachments,
    )


async def _execute_calendar_action(
    action: PendingAction, current_user: User, db: AsyncSession
) -> dict[str, object]:
    connector = await _get_connector("google_calendar", current_user, db)
    if connector is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Google Calendar isn't connected")
    # Checked before calling Google so the action stays pending: the user can reconnect
    # (granting write access) and then confirm this same draft.
    if not google_calendar_events.can_write(connector.scopes):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Google Calendar is connected read-only. Reconnect it on the Connectors page to "
            "let your twin change your calendar, then confirm again.",
        )

    access_token = await get_valid_access_token(connector, db)
    result = await google_calendar_events.execute(action.tool_name, access_token, action.payload)
    if action.tool_name == "delete_calendar_event":
        # Don't let the twin keep "remembering" an event the user just deleted. (A deleted
        # series leaves its occurrences' rows until they fall out of the sync window.)
        await db.execute(
            delete(MemoryItem).where(
                MemoryItem.user_id == current_user.id,
                MemoryItem.source == "google_calendar",
                MemoryItem.external_id == action.payload["event_id"],
            )
        )
    return result


_EXECUTORS = {
    "draft_email": _execute_draft_email,
    **{name: _execute_calendar_action for name in google_calendar_events.WRITE_TOOLS},
}


@router.get("/actions/{action_id}", response_model=PendingActionRead)
async def get_action(
    action_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PendingAction:
    return await _get_owned_action(action_id, current_user, db)


@router.post("/actions/{action_id}/confirm", response_model=PendingActionRead)
async def confirm_action(
    action_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PendingAction:
    action = await _get_owned_action(action_id, current_user, db)
    if action.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Action is already {action.status}")

    executor = _EXECUTORS.get(action.tool_name)
    if executor is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown tool: {action.tool_name}")

    try:
        action.result = await executor(action, current_user, db)
        action.status = "executed"
    except ConnectorAccessExpired as exc:
        # Not the draft's fault: keep it pending so the user can reconnect and confirm the
        # same draft again instead of asking the twin to write it all over.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        action.status = "failed"
        action.result = {"error": str(exc)}
    await db.commit()
    await db.refresh(action)
    return action


@router.post("/actions/{action_id}/cancel", response_model=PendingActionRead)
async def cancel_action(
    action_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PendingAction:
    action = await _get_owned_action(action_id, current_user, db)
    if action.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Action is already {action.status}")

    action.status = "cancelled"
    await db.commit()
    await db.refresh(action)
    return action
