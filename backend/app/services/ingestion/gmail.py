"""Gmail ingestion: full message bodies plus readable attachments, synced incrementally.

The first poll records Gmail's current historyId and starts a backfill of the last
`gmail_backfill_days` of mail, one page per poll. Every poll also replays Gmail's history
since the stored historyId, so new mail lands within one poll even while the backfill runs.
"""

import asyncio
import base64
import html
import logging
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import PurePath
from typing import Any

import httpx

from app.core.config import get_settings
from app.services.ingestion import documents
from app.services.ingestion.base import NormalizedItem, SyncBatch, parse_email_date

settings = get_settings()
logger = logging.getLogger(__name__)

_API = "https://gmail.googleapis.com/gmail/v1/users/me"
_PROFILE_ENDPOINT = f"{_API}/profile"
_HISTORY_ENDPOINT = f"{_API}/history"
_LIST_ENDPOINT = f"{_API}/messages"
_MESSAGE_ENDPOINT = _API + "/messages/{id}"
_ATTACHMENT_ENDPOINT = _API + "/messages/{message_id}/attachments/{attachment_id}"

# Enough for the substance of almost any email; long newsletters are cut rather than
# flooding search and the chat model's context.
_BODY_MAX_CHARS = 4000
# Drafts (every autosave is a new message), spam, trash and Hangouts chats aren't memories.
_SKIP_LABELS = {"DRAFT", "SPAM", "TRASH", "CHAT"}
# New mail beyond this in one poll is carried over to the next one.
_MAX_NEW_PER_POLL = 200
# Gmail allows ~50 message reads/second per user; stay well under it.
_CONCURRENCY = 8
_ATTACHMENT_FORMATS = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".txt": "text",
    ".md": "text",
    ".markdown": "text",
}
_ATTACHMENT_MAX_CHUNKS = 50


class _HistoryExpired(Exception):
    """Gmail no longer has history that far back (it keeps roughly a week)."""


def http_client() -> httpx.AsyncClient:
    """Every Gmail API call goes through this, so tests can swap in a mock transport."""
    return httpx.AsyncClient(timeout=30)


def _auth(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


# --- message parsing -----------------------------------------------------------------------


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def iter_parts(part: dict) -> list[dict]:
    """The part and all its descendants, depth first (a message payload is a MIME tree)."""
    parts = [part]
    for child in part.get("parts", []) or []:
        parts.extend(iter_parts(child))
    return parts


def _headers(part: dict) -> dict[str, str]:
    found: dict[str, str] = {}
    for header in part.get("headers", []) or []:
        name = str(header.get("name", "")).lower()
        if name and name not in found and header.get("value") is not None:
            found[name] = str(header["value"])
    return found


def _is_attachment(part: dict) -> bool:
    body = part.get("body", {}) or {}
    return bool(part.get("filename")) and bool(body.get("attachmentId") or body.get("data"))


def _part_text(part: dict) -> str:
    data = (part.get("body", {}) or {}).get("data")
    if not data:
        return ""
    raw = _b64decode(data)
    match = re.search(r'charset="?([^";\s]+)', _headers(part).get("content-type", ""), re.I)
    try:
        return raw.decode(match.group(1) if match else "utf-8", errors="replace")
    except LookupError:  # an unknown charset name
        return raw.decode("utf-8", errors="replace")


class _HTMLText(HTMLParser):
    """HTML body → readable text: block elements become line breaks, scripts/styles and
    quoted replies (Gmail's .gmail_quote, <blockquote>) are dropped."""

    _BLOCK = {"p", "div", "br", "tr", "li", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5", "h6"}
    _HIDDEN = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0
        self._quote_tag: str | None = None
        self._quote_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._quote_tag is not None:
            if tag == self._quote_tag:
                self._quote_depth += 1
            return
        classes = (dict(attrs).get("class") or "").split()
        if tag == "blockquote" or "gmail_quote" in classes:
            self._quote_tag, self._quote_depth = tag, 1
        elif tag in self._HIDDEN:
            self._hidden += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._quote_tag is not None:
            if tag == self._quote_tag:
                self._quote_depth -= 1
                if self._quote_depth == 0:
                    self._quote_tag = None
            return
        if tag in self._HIDDEN:
            self._hidden = max(0, self._hidden - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden and self._quote_tag is None:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _HTMLText()
    parser.feed(markup)
    parser.close()
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in "".join(parser.parts).split("\n"))
    return "\n".join(lines)


# Where a reply's quoted history starts: Gmail/Apple ("On <date>, <name> wrote:", sometimes
# wrapped onto a second line) and Outlook ("-----Original Message-----" or a rule of
# underscores above the From:/Sent: block). Forwarded messages are kept — they're content.
_QUOTE_START = re.compile(
    r"^(?:On\b[^\n]{0,250}(?:\n[^\n]{0,250})?\bwrote:[ \t]*$"
    r"|-{2,}\s*Original Message\s*-{2,}[ \t]*$"
    r"|_{20,}[ \t]*$)",
    re.M | re.I,
)


def strip_quoted_reply(text: str) -> str:
    match = _QUOTE_START.search(text)
    if match and text[: match.start()].strip():
        text = text[: match.start()]
    return "\n".join(line for line in text.split("\n") if not line.lstrip().startswith(">"))


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > _BODY_MAX_CHARS:
        text = text[:_BODY_MAX_CHARS].rstrip() + "…"
    return text


def extract_body(payload: dict) -> str:
    """The message's own words: its text/plain part (or text/html converted), without the
    quoted history of earlier replies."""
    plain, markup = [], []
    for part in iter_parts(payload):
        if _is_attachment(part):
            continue
        mime = str(part.get("mimeType", "")).lower()
        if mime == "text/plain":
            plain.append(_part_text(part))
        elif mime == "text/html":
            markup.append(_part_text(part))
    if any(p.strip() for p in plain):
        text = "\n\n".join(plain)
    elif markup:
        text = "\n\n".join(html_to_text(m) for m in markup)
    else:
        return ""
    return _clean(strip_quoted_reply(text.replace("\r\n", "\n")))


def list_attachments(payload: dict) -> list[dict[str, Any]]:
    return [
        {
            "part_id": part.get("partId"),
            "filename": part["filename"],
            "mime_type": part.get("mimeType"),
            "size": (part.get("body", {}) or {}).get("size"),
        }
        for part in iter_parts(payload)
        if _is_attachment(part)
    ]


def find_part(payload: dict, part_id: str) -> dict | None:
    return next((p for p in iter_parts(payload) if p.get("partId") == part_id), None)


def _sent_at(message: dict, headers: dict[str, str]) -> datetime | None:
    # internalDate (ms since epoch, set by Gmail) is more reliable than the sender's Date header.
    internal = message.get("internalDate")
    if internal and str(internal).isdigit():
        return datetime.fromtimestamp(int(internal) / 1000, tz=UTC)
    return parse_email_date(headers.get("date"))


def normalize_message(message: dict) -> NormalizedItem:
    payload = message.get("payload", {}) or {}
    headers = _headers(payload)
    subject = headers.get("subject") or "(no subject)"
    body = extract_body(payload) or html.unescape(message.get("snippet", ""))
    attachments = list_attachments(payload)
    content = f"{subject}\n\n{body}"
    if attachments:
        content += "\n\nAttachments: " + ", ".join(a["filename"] for a in attachments)
    return NormalizedItem(
        external_id=message["id"],
        content=content,
        item_metadata={
            "subject": subject,
            "from": headers.get("from"),
            "to": headers.get("to"),
            "cc": headers.get("cc"),
            "date": headers.get("date"),
            # What a reply needs to land in the same thread on both ends.
            "thread_id": message.get("threadId"),
            "message_id_header": headers.get("message-id"),
            "references": headers.get("references"),
            "labels": message.get("labelIds", []),
            "attachments": attachments,
        },
        occurred_at=_sent_at(message, headers),
    )


# --- API calls -----------------------------------------------------------------------------


async def get_message(client: httpx.AsyncClient, access_token: str, message_id: str) -> dict | None:
    """The full message, or None if it was deleted in the meantime."""
    response = await client.get(
        _MESSAGE_ENDPOINT.format(id=message_id),
        headers=_auth(access_token),
        params={"format": "full"},
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return dict(response.json())


async def download_part(
    client: httpx.AsyncClient, access_token: str, message_id: str, part: dict
) -> bytes:
    """An attachment's bytes. Small parts come inline; the rest need a second request (and
    their attachmentId changes on every fetch, so it must come from a fresh message read)."""
    body = part.get("body", {}) or {}
    if body.get("data"):
        return _b64decode(body["data"])
    response = await client.get(
        _ATTACHMENT_ENDPOINT.format(message_id=message_id, attachment_id=body["attachmentId"]),
        headers=_auth(access_token),
    )
    response.raise_for_status()
    return _b64decode(response.json()["data"])


def attachment_format(filename: str, mime_type: str | None) -> str | None:
    """How to read an attachment's text, or None if it isn't a kind we index."""
    fmt = _ATTACHMENT_FORMATS.get(PurePath(filename).suffix.lower())
    if fmt is None and mime_type == "application/pdf":
        fmt = "pdf"
    return fmt


def _extract(fmt: str, data: bytes) -> documents.ExtractedText:
    if fmt == "pdf":
        return documents.extract_pdf(data)
    if fmt == "docx":
        return documents.extract_docx(data)
    return documents.decode_text(data)


async def _attachment_items(
    client: httpx.AsyncClient, access_token: str, message: dict, email: NormalizedItem
) -> list[NormalizedItem]:
    """Readable attachments (PDF, Word, text), chunked like an uploaded document so the twin
    can find e.g. a certificate that only ever arrived by mail."""
    items = []
    for part in iter_parts(message.get("payload", {}) or {}):
        if not _is_attachment(part):
            continue
        filename = str(part["filename"])
        size = (part.get("body", {}) or {}).get("size") or 0
        fmt = attachment_format(filename, part.get("mimeType"))
        if fmt is None or size > settings.upload_max_bytes:
            continue
        data = await download_part(client, access_token, message["id"], part)
        try:
            extracted = await asyncio.to_thread(_extract, fmt, data)
        except documents.DocumentUnreadable:
            logger.info("Skipping unreadable attachment %r in %s", filename, message["id"])
            continue
        chunks = documents.chunk_text(extracted.text)[:_ATTACHMENT_MAX_CHUNKS]
        for index, chunk in enumerate(chunks):
            title = (
                filename if len(chunks) == 1 else f"{filename} (part {index + 1} of {len(chunks)})"
            )
            items.append(
                NormalizedItem(
                    external_id=f"{message['id']}:{part.get('partId')}:{index}",
                    content=f"{title}\n\n{chunk}",
                    item_metadata={
                        "kind": "attachment",
                        "filename": filename,
                        "mime_type": part.get("mimeType"),
                        "size": size,
                        "chunk_index": index,
                        "chunk_count": len(chunks),
                        "message_id": message["id"],
                        "part_id": part.get("partId"),
                        "email_subject": email.item_metadata["subject"],
                        "from": email.item_metadata["from"],
                    },
                    occurred_at=email.occurred_at,
                )
            )
    return items


async def _items_for(
    client: httpx.AsyncClient, access_token: str, message_ids: list[str]
) -> list[NormalizedItem]:
    semaphore = asyncio.Semaphore(_CONCURRENCY)

    async def one(message_id: str) -> list[NormalizedItem]:
        async with semaphore:
            message = await get_message(client, access_token, message_id)
            if message is None or _SKIP_LABELS & set(message.get("labelIds", []) or []):
                return []
            email = normalize_message(message)
            return [email, *await _attachment_items(client, access_token, message, email)]

    results = await asyncio.gather(*(one(message_id) for message_id in message_ids))
    return [item for items in results for item in items]


async def _start_backfill(client: httpx.AsyncClient, access_token: str) -> dict[str, Any]:
    # Taking the historyId *before* listing old mail means nothing that arrives during the
    # backfill can fall between the two.
    response = await client.get(_PROFILE_ENDPOINT, headers=_auth(access_token))
    response.raise_for_status()
    return {
        "history_id": str(response.json()["historyId"]),
        "backfill": {"page_token": None, "fetched": 0},
    }


async def _history_since(
    client: httpx.AsyncClient, access_token: str, history_id: str
) -> tuple[list[str], str]:
    """Ids of messages added since `history_id`, and the historyId to resume from next."""
    params: dict[str, str | int] = {
        "startHistoryId": history_id,
        "historyTypes": "messageAdded",
        "maxResults": 500,
    }
    ids: list[str] = []
    latest = history_id
    while True:
        response = await client.get(_HISTORY_ENDPOINT, headers=_auth(access_token), params=params)
        if response.status_code == 404:
            raise _HistoryExpired
        response.raise_for_status()
        page = response.json()
        latest = str(page.get("historyId") or latest)
        for record in page.get("history", []) or []:
            for added in record.get("messagesAdded", []) or []:
                message = added.get("message", {}) or {}
                if message.get("id") and not _SKIP_LABELS & set(message.get("labelIds", []) or []):
                    ids.append(message["id"])
        if not page.get("nextPageToken"):
            return ids, latest
        params["pageToken"] = page["nextPageToken"]


async def _backfill_page(
    client: httpx.AsyncClient, access_token: str, page_token: str | None, page_size: int
) -> tuple[list[str], str | None]:
    params: dict[str, str | int] = {
        "q": f"newer_than:{settings.gmail_backfill_days}d -in:chats -in:drafts",
        "maxResults": page_size,
    }
    if page_token:
        params["pageToken"] = page_token
    response = await client.get(_LIST_ENDPOINT, headers=_auth(access_token), params=params)
    response.raise_for_status()
    page = response.json()
    ids = [m["id"] for m in page.get("messages", []) or []]
    return ids, page.get("nextPageToken")


async def fetch_batch(access_token: str, sync_state: dict[str, Any]) -> SyncBatch:
    """One poll: new mail since the last poll, plus the next backfill page if one is due."""
    state = dict(sync_state)
    async with http_client() as client:
        ids: list[str] = list(state.pop("pending_ids", []) or [])
        if "history_id" not in state:
            state = await _start_backfill(client, access_token)
        else:
            try:
                new_ids, state["history_id"] = await _history_since(
                    client, access_token, state["history_id"]
                )
                ids.extend(new_ids)
            except _HistoryExpired:
                # Polling stopped for over a week. Start over; upserts make re-fetching mail
                # we already have harmless, and unchanged emails aren't re-embedded.
                state, ids = await _start_backfill(client, access_token), []

        ids = list(dict.fromkeys(ids))
        to_fetch, carried_over = ids[:_MAX_NEW_PER_POLL], ids[_MAX_NEW_PER_POLL:]
        if carried_over:
            state["pending_ids"] = carried_over

        backfill = state.get("backfill")
        if backfill is not None:
            fetched = int(backfill.get("fetched", 0))
            page_size = min(
                settings.gmail_sync_page_size, settings.gmail_backfill_max_messages - fetched
            )
            page_ids, next_token = (
                await _backfill_page(client, access_token, backfill.get("page_token"), page_size)
                if page_size > 0
                else ([], None)
            )
            to_fetch = list(dict.fromkeys([*to_fetch, *page_ids]))
            fetched += len(page_ids)
            if next_token and fetched < settings.gmail_backfill_max_messages:
                state["backfill"] = {"page_token": next_token, "fetched": fetched}
            else:
                state.pop("backfill")

        items = await _items_for(client, access_token, to_fetch)
    return SyncBatch(items, state)
