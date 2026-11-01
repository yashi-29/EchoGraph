"""Turns the chat model's draft_email arguments into the draft the user reviews, and the
confirmed draft back into a sendable message.

The model refers to emails and files by the ids search_memory showed it. Drafting resolves
those ids against the user's own memories (so the card can show exactly what will be
attached and which thread a reply lands in); the file bytes are loaded only on confirm.
"""

import re
import uuid
from email.utils import formataddr, getaddresses
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.memory_item import MemoryItem
from app.models.upload import Upload
from app.services.actions.gmail_send import Attachment
from app.services.ingestion import gmail as gmail_api

# Template leftovers like [Your Name], [Recipient's Name], [Date] — never send these.
_PLACEHOLDER = re.compile(
    r"\[(?:your|my|recipient|sender|their|his|her|name|date|company|insert)\b[^\]\n]{0,40}\]",
    re.I,
)

# What most recipients' mail servers accept for one message.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


def _addresses(value: object, field: str) -> list[str]:
    """'a@x.com, "Doe, Jo" <jo@y.com>' (or a list of them) → validated, formatted addresses."""
    raw = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value or "")
    parsed = getaddresses([raw])
    addresses = [formataddr((name, addr)) for name, addr in parsed if addr]
    if any("@" not in addr for _, addr in parsed if addr) or (raw.strip() and not addresses):
        raise ValueError(
            f"'{field}' must be email addresses, got {raw!r} — search_memory for this person's "
            "email, or ask the user for it"
        )
    return addresses


async def _memory_by_id(external_id: str, user_id: uuid.UUID, db: AsyncSession) -> MemoryItem:
    item = (
        await db.execute(
            select(MemoryItem)
            .where(MemoryItem.user_id == user_id, MemoryItem.external_id == external_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if item is None:
        raise ValueError(
            f"No memory has id {external_id!r} — call search_memory and use an id from its results"
        )
    return item


async def _reply_context(reply_to_id: str, user_id: uuid.UUID, db: AsyncSession) -> dict[str, Any]:
    item = await _memory_by_id(reply_to_id, user_id, db)
    meta = item.item_metadata or {}
    if item.source != "gmail":
        raise ValueError(f"{reply_to_id!r} isn't an email — reply_to_id must be a Gmail message id")
    if meta.get("kind") == "attachment":  # the model passed one of the email's attachments
        item = await _memory_by_id(str(meta["message_id"]), user_id, db)
        meta = item.item_metadata or {}
    return {
        "message_id": item.external_id,
        "thread_id": meta.get("thread_id"),
        "in_reply_to": meta.get("message_id_header"),
        "references": meta.get("references"),
        "subject": meta.get("subject"),
        # Replying to something the user sent goes back to the people they sent it to.
        "default_to": meta.get("to") if "SENT" in (meta.get("labels") or []) else meta.get("from"),
    }


def _gmail_attachments(item: MemoryItem) -> list[dict[str, Any]]:
    meta = item.item_metadata or {}
    if meta.get("kind") == "attachment":
        refs = [meta]
        message_id = str(meta["message_id"])
    else:  # a whole email: attach everything it carried
        refs = meta.get("attachments") or []
        message_id = item.external_id
    return [
        {
            "source": "gmail",
            "message_id": message_id,
            "part_id": ref.get("part_id"),
            "filename": ref.get("filename"),
            "mime_type": ref.get("mime_type") or "application/octet-stream",
            "size": ref.get("size") or 0,
        }
        for ref in refs
    ]


async def _attachment_refs(ids: list[str], user_id: uuid.UUID, db: AsyncSession) -> list[dict]:
    refs: dict[tuple[str, ...], dict[str, Any]] = {}
    for external_id in ids:
        item = await _memory_by_id(str(external_id), user_id, db)
        if item.upload_id is not None:
            upload = await db.get(Upload, item.upload_id)
            if upload is None:
                raise ValueError(f"The file behind {external_id!r} was deleted")
            refs[("upload", str(upload.id))] = {
                "source": "upload",
                "upload_id": str(upload.id),
                "filename": upload.filename,
                "mime_type": upload.content_type,
                "size": upload.size_bytes,
            }
        elif item.source == "gmail":
            found = _gmail_attachments(item)
            if not found:
                raise ValueError(
                    f"The email {external_id!r} has no attachments — pass the id of an uploaded "
                    "document or an email attachment from search_memory instead"
                )
            for ref in found:
                refs[("gmail", ref["message_id"], str(ref["part_id"]))] = ref
        else:
            raise ValueError(f"{external_id!r} is not a file that can be attached")

    total = sum(int(ref["size"] or 0) for ref in refs.values())
    if total > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"Those attachments add up to {total // 2**20} MB — the limit is "
            f"{MAX_ATTACHMENT_BYTES // 2**20} MB. Attach fewer files."
        )
    return list(refs.values())


async def prepare_draft(args: dict[str, Any], user_id: uuid.UUID, db: AsyncSession) -> dict:
    """The draft the user will review, or ValueError telling the model what to fix."""
    reply = None
    if args.get("reply_to_id"):
        reply = await _reply_context(str(args["reply_to_id"]), user_id, db)

    to = _addresses(args.get("to") or (reply or {}).get("default_to") or "", "to")
    if not to:
        raise ValueError(
            "'to' is required — search_memory for this person's email, or ask the user for it"
        )
    cc = _addresses(args.get("cc") or "", "cc")

    subject = str(args.get("subject") or "").strip()
    if reply is not None:
        original = str(reply.get("subject") or "").strip()
        if not subject:
            subject = original
        if subject and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

    body = str(args.get("body") or "")
    placeholders = list(dict.fromkeys(_PLACEHOLDER.findall(f"{subject}\n{body}")))
    if placeholders:
        raise ValueError(
            f"Remove the placeholder(s) {', '.join(placeholders)}: fill them in from what you "
            "know (sign with the user's name if you know it) or drop that part — e.g. end "
            "with just 'Best regards,'"
        )

    payload: dict[str, Any] = {
        "to": ", ".join(to),
        "subject": subject,
        "body": body,
    }
    if cc:
        payload["cc"] = cc
    if reply is not None:
        payload["reply_to"] = {k: v for k, v in reply.items() if k != "default_to"}
    attachment_ids = args.get("attachment_ids") or []
    if isinstance(attachment_ids, str):
        attachment_ids = [attachment_ids]
    if attachment_ids:
        payload["attachments"] = await _attachment_refs(attachment_ids, user_id, db)
    return payload


async def load_attachments(
    refs: list[dict[str, Any]], user_id: uuid.UUID, db: AsyncSession, access_token: str
) -> list[Attachment]:
    """The confirmed draft's files, read fresh: uploads from the database, email attachments
    from Gmail. RuntimeError (shown to the user) if one has gone since it was drafted."""
    attachments = []
    messages: dict[str, dict | None] = {}
    async with gmail_api.http_client() as client:
        for ref in refs:
            if ref["source"] == "upload":
                data = await db.scalar(
                    select(Upload.data).where(
                        Upload.id == uuid.UUID(ref["upload_id"]), Upload.user_id == user_id
                    )
                )
                if data is None:
                    raise RuntimeError(f"{ref['filename']} was deleted from your uploads")
            else:
                message_id = ref["message_id"]
                if message_id not in messages:
                    messages[message_id] = await gmail_api.get_message(
                        client, access_token, message_id
                    )
                message = messages[message_id]
                part = gmail_api.find_part(message["payload"], ref["part_id"]) if message else None
                if part is None:
                    raise RuntimeError(
                        f"The email carrying {ref['filename']} is no longer in Gmail"
                    )
                data = await gmail_api.download_part(client, access_token, message_id, part)
            attachments.append(Attachment(ref["filename"], ref["mime_type"], data))
    return attachments
