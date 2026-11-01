import base64
import email
import io
import json
import uuid
import zipfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from email import policy
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import app.services.ingestion.gmail as gmail_module
import app.services.ingestion.sync as sync_module
from app.core.crypto import encrypt_token
from app.db.session import async_session_factory
from app.main import app
from app.models.connector import Connector
from app.models.memory_item import EMBEDDING_DIM, MemoryItem
from app.models.pending_action import PendingAction
from app.models.upload import Upload
from app.models.user import User
from app.services.actions import email_drafts, gmail_send
from app.services.ingestion import documents
from app.services.ingestion.base import SyncBatch


def _b64(data: bytes | str) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _headers(**values: str) -> list[dict[str, str]]:
    return [{"name": name.replace("_", "-"), "value": value} for name, value in values.items()]


def _message(
    message_id: str,
    *,
    plain: str | None = None,
    html: str | None = None,
    attachment: tuple[str, str, str] | None = None,  # (filename, mime type, attachmentId)
    labels: tuple[str, ...] = ("INBOX",),
    subject: str = "Hello",
) -> dict[str, Any]:
    alternatives = []
    if plain is not None:
        alternatives.append(
            {
                "partId": "0.0",
                "mimeType": "text/plain",
                "headers": _headers(Content_Type="text/plain; charset=UTF-8"),
                "body": {"data": _b64(plain), "size": len(plain)},
            }
        )
    if html is not None:
        alternatives.append(
            {"partId": "0.1", "mimeType": "text/html", "body": {"data": _b64(html)}}
        )
    parts: list[dict[str, Any]] = [
        {"partId": "0", "mimeType": "multipart/alternative", "parts": alternatives, "body": {}}
    ]
    if attachment is not None:
        filename, mime_type, attachment_id = attachment
        parts.append(
            {
                "partId": "1",
                "mimeType": mime_type,
                "filename": filename,
                "body": {"attachmentId": attachment_id, "size": 40},
            }
        )
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "labelIds": list(labels),
        "snippet": "snippet text",
        "internalDate": "1790000000000",
        "payload": {
            "partId": "",
            "mimeType": "multipart/mixed",
            "headers": _headers(
                Subject=subject,
                From="Aditya <aditya@example.com>",
                To="Yashi <yashi@example.com>",
                Message_ID=f"<{message_id}@mail.example.com>",
            ),
            "parts": parts,
            "body": {},
        },
    }


Handler = Callable[[httpx.Request], httpx.Response]


def _mock_gmail(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> list[httpx.Request]:
    """Routes every Gmail API call through `handler`; returns the log of requests made."""
    seen: list[httpx.Request] = []

    def logging_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        gmail_module,
        "http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(logging_handler)),
    )
    return seen


# --- parsing -------------------------------------------------------------------------------


def test_body_prefers_plain_text_and_drops_quoted_history() -> None:
    plain = (
        "Hi Yashi,\r\n\r\nYour NOC is attached.\r\n\r\n"
        "On Mon, 5 Oct 2026 at 10:00, Aditya <aditya@example.com>\r\nwrote:\r\n"
        "> Can you send the NOC?\r\n"
    )
    message = _message("m1", plain=plain, html="<p>ignored when plain text exists</p>")
    item = gmail_module.normalize_message(message)

    assert item.content == "Hello\n\nHi Yashi,\n\nYour NOC is attached."
    assert item.item_metadata["thread_id"] == "thread-m1"
    assert item.item_metadata["message_id_header"] == "<m1@mail.example.com>"
    assert item.item_metadata["attachments"] == []


def test_html_only_body_is_converted_without_quotes_or_scripts() -> None:
    html = (
        "<html><head><style>p{color:red}</style></head><body>"
        "<div>Meeting moved to <b>Friday</b>.</div><div>Thanks,<br>Neha</div>"
        '<div class="gmail_quote"><div>On Sunday you wrote:</div><div>old text</div></div>'
        "<script>alert(1)</script></body></html>"
    )
    item = gmail_module.normalize_message(_message("m2", html=html))

    assert item.content == "Hello\n\nMeeting moved to Friday.\n\nThanks,\nNeha"


def test_attachments_are_listed_in_content_and_metadata() -> None:
    message = _message("m3", plain="See attached.", attachment=("NOC.pdf", "application/pdf", "a1"))
    item = gmail_module.normalize_message(message)

    assert item.content.endswith("\n\nAttachments: NOC.pdf")
    assert item.item_metadata["attachments"] == [
        {"part_id": "1", "filename": "NOC.pdf", "mime_type": "application/pdf", "size": 40}
    ]


def test_extract_docx_reads_paragraphs() -> None:
    word = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xml = (
        f'<w:document xmlns:w="{word}"><w:body>'
        "<w:p><w:r><w:t>Internship </w:t></w:r><w:r><w:t>certificate</w:t></w:r></w:p>"
        "<w:p></w:p>"
        "<w:p><w:r><w:t>Issued to Yashi</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)

    assert documents.extract_docx(buffer.getvalue()).text == (
        "Internship certificate\n\nIssued to Yashi"
    )
    with pytest.raises(documents.DocumentUnreadable):
        documents.extract_docx(b"not a zip")


# --- incremental sync ----------------------------------------------------------------------


def _json(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=payload)


async def test_first_poll_starts_backfill_and_indexes_bodies_and_attachments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = {
        "m1": _message(
            "m1", plain="Your NOC is attached.", attachment=("NOC.txt", "text/plain", "att-1")
        ),
        "d1": _message("d1", plain="half-written", labels=("DRAFT",)),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/profile"):
            return _json({"historyId": "100"})
        if path.endswith("/messages"):
            assert "newer_than:" in request.url.params["q"]
            return _json({"messages": [{"id": "m1"}, {"id": "d1"}], "nextPageToken": "page-2"})
        if path.endswith("/attachments/att-1"):
            return _json({"data": _b64("No objection certificate for Yashi")})
        return _json(messages[path.rsplit("/", 1)[-1]])

    _mock_gmail(monkeypatch, handler)
    batch = await gmail_module.fetch_batch("token", {})

    assert batch.sync_state == {
        "history_id": "100",
        "backfill": {"page_token": "page-2", "fetched": 2},
    }
    by_id = {item.external_id: item for item in batch.items}
    assert set(by_id) == {"m1", "m1:1:0"}  # the draft is skipped
    attachment = by_id["m1:1:0"]
    assert attachment.content == "NOC.txt\n\nNo objection certificate for Yashi"
    assert attachment.item_metadata["kind"] == "attachment"
    assert attachment.item_metadata["message_id"] == "m1"
    assert attachment.item_metadata["email_subject"] == "Hello"
    assert attachment.occurred_at == by_id["m1"].occurred_at


async def test_later_poll_replays_history_and_finishes_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/history"):
            assert request.url.params["startHistoryId"] == "100"
            added = [
                {"message": {"id": "m3", "labelIds": ["INBOX"]}},
                {"message": {"id": "d2", "labelIds": ["DRAFT"]}},
            ]
            return _json({"history": [{"messagesAdded": added}], "historyId": "120"})
        if path.endswith("/messages"):
            assert request.url.params["pageToken"] == "page-2"
            return _json({"messages": [{"id": "m4"}]})
        message_id = path.rsplit("/", 1)[-1]
        assert message_id != "d2", "drafts are filtered out before they're fetched"
        return _json(_message(message_id, plain=f"body of {message_id}"))

    _mock_gmail(monkeypatch, handler)
    state = {"history_id": "100", "backfill": {"page_token": "page-2", "fetched": 2}}
    batch = await gmail_module.fetch_batch("token", state)

    assert batch.sync_state == {"history_id": "120"}  # backfill done: no next page
    assert [item.external_id for item in batch.items] == ["m3", "m4"]


async def test_expired_history_restarts_the_backfill(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/history"):
            return httpx.Response(404, json={"error": {"code": 404}})
        if path.endswith("/profile"):
            return _json({"historyId": "900"})
        if path.endswith("/messages"):
            return _json({"messages": []})
        raise AssertionError(f"unexpected call {path}")

    _mock_gmail(monkeypatch, handler)
    batch = await gmail_module.fetch_batch("token", {"history_id": "1"})

    assert batch.sync_state == {"history_id": "900"}
    assert batch.items == []


async def test_a_flood_of_new_mail_is_carried_over_to_the_next_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gmail_module, "_MAX_NEW_PER_POLL", 2)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/history"):
            added = [{"message": {"id": f"n{i}", "labelIds": ["INBOX"]}} for i in range(3)]
            return _json({"history": [{"messagesAdded": added}], "historyId": "7"})
        return _json(_message(request.url.path.rsplit("/", 1)[-1], plain="x"))

    _mock_gmail(monkeypatch, handler)
    first = await gmail_module.fetch_batch("token", {"history_id": "5"})
    assert [i.external_id for i in first.items] == ["n0", "n1"]
    assert first.sync_state == {"history_id": "7", "pending_ids": ["n2"]}


async def test_sync_connector_persists_the_new_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    async with async_session_factory() as db:
        user = User(email=f"{uuid.uuid4()}@example.com", hashed_password="x")
        db.add(user)
        await db.flush()
        connector = Connector(
            user_id=user.id, provider="gmail", access_token_encrypted=encrypt_token("t")
        )
        db.add(connector)
        await db.commit()
        connector_id = connector.id

    received: list[dict] = []

    async def fake_fetch(_token: str, sync_state: dict) -> SyncBatch:
        received.append(sync_state)
        return SyncBatch([], {"history_id": str(len(received))})

    monkeypatch.setitem(sync_module._PROVIDER_FETCHERS, "gmail", fake_fetch)
    for _ in range(2):
        async with async_session_factory() as db:
            db_connector = await db.get(Connector, connector_id)
            assert db_connector is not None
            await sync_module.sync_connector(db_connector, db)

    assert received == [{}, {"history_id": "1"}]
    async with async_session_factory() as db:
        stored = await db.get(Connector, connector_id)
        assert stored is not None and stored.sync_state == {"history_id": "2"}


# --- drafting and sending ------------------------------------------------------------------


async def _user_with_mail() -> tuple[uuid.UUID, uuid.UUID]:
    """A user with one email (carrying an attachment) and one uploaded document."""
    async with async_session_factory() as db:
        user = User(email=f"{uuid.uuid4()}@example.com", hashed_password="x")
        db.add(user)
        await db.flush()
        upload = Upload(
            user_id=user.id, kind="document", filename="NOC.pdf",
            content_type="application/pdf", size_bytes=11, sha256=uuid.uuid4().hex * 2,
            data=b"%PDF-1.4 ok",
        )  # fmt: skip
        db.add(upload)
        await db.flush()
        vector = [0.0] * EMBEDDING_DIM
        email_meta = {
            "subject": "Internship documents",
            "from": "Aditya <aditya@example.com>",
            "to": "Yashi <yashi@example.com>",
            "thread_id": "thread-m1",
            "message_id_header": "<m1@mail.example.com>",
            "references": "<m0@mail.example.com>",
            "labels": ["INBOX"],
            "attachments": [
                {"part_id": "1", "filename": "offer.pdf", "mime_type": "application/pdf", "size": 9}
            ],
        }
        db.add_all(
            [
                MemoryItem(
                    user_id=user.id, source="gmail", external_id="m1",
                    content="Internship documents\n\nSee attached", item_metadata=email_meta,
                    embedding=vector,
                ),
                MemoryItem(
                    user_id=user.id, source="gmail", external_id="m1:1:0",
                    content="offer.pdf\n\nOffer letter", embedding=vector,
                    item_metadata={
                        "kind": "attachment", "filename": "offer.pdf",
                        "mime_type": "application/pdf", "size": 9, "message_id": "m1",
                        "part_id": "1", "chunk_index": 0,
                    },
                ),
                MemoryItem(
                    user_id=user.id, source="document", external_id=f"{upload.id}:0",
                    content="NOC.pdf\n\nNo objection", item_metadata={"filename": "NOC.pdf"},
                    embedding=vector, upload_id=upload.id,
                ),
            ]
        )  # fmt: skip
        await db.commit()
        return user.id, upload.id


async def test_prepare_draft_resolves_reply_thread_and_attachments() -> None:
    user_id, upload_id = await _user_with_mail()
    async with async_session_factory() as db:
        draft = await email_drafts.prepare_draft(
            {
                "body": "Thanks, both attached.",
                "cc": "neha@example.com",
                "reply_to_id": "m1",
                "attachment_ids": [f"{upload_id}:0", "m1:1:0", f"{upload_id}:0"],
            },
            user_id,
            db,
        )

    assert draft["to"] == "Aditya <aditya@example.com>"  # replies go back to the sender
    assert draft["cc"] == ["neha@example.com"]
    assert draft["subject"] == "Re: Internship documents"
    assert draft["reply_to"] == {
        "message_id": "m1",
        "thread_id": "thread-m1",
        "in_reply_to": "<m1@mail.example.com>",
        "references": "<m0@mail.example.com>",
        "subject": "Internship documents",
    }
    assert [(a["source"], a["filename"]) for a in draft["attachments"]] == [
        ("upload", "NOC.pdf"),
        ("gmail", "offer.pdf"),
    ]


async def test_prepare_draft_rejects_bad_addresses_and_unknown_ids() -> None:
    user_id, _ = await _user_with_mail()
    async with async_session_factory() as db:
        with pytest.raises(ValueError, match="'to' must be email addresses"):
            await email_drafts.prepare_draft({"to": "aditya", "body": "hi"}, user_id, db)
        with pytest.raises(ValueError, match="No memory has id"):
            await email_drafts.prepare_draft(
                {"to": "a@example.com", "body": "hi", "attachment_ids": ["nope"]}, user_id, db
            )


def test_build_message_threads_the_reply_and_attaches_files() -> None:
    raw = gmail_send.build_message(
        "a@example.com",
        "Re: Plans",
        "Sounds good.",
        cc=["b@example.com"],
        in_reply_to="<m1@x>",
        references="<m0@x>",
        attachments=[gmail_send.Attachment("NOC.pdf", "application/pdf", b"%PDF")],
    )
    parsed = email.message_from_bytes(raw, policy=policy.default)

    assert parsed["Cc"] == "b@example.com"
    assert parsed["In-Reply-To"] == "<m1@x>"
    assert parsed["References"] == "<m0@x> <m1@x>"
    [attachment] = list(parsed.iter_attachments())  # type: ignore[attr-defined]
    assert attachment.get_filename() == "NOC.pdf"
    assert attachment.get_content() == b"%PDF"


@asynccontextmanager
async def _signed_up_client() -> AsyncIterator[tuple[AsyncClient, uuid.UUID]]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        signup = await client.post(
            "/api/v1/auth/signup",
            json={"email": f"{uuid.uuid4()}@example.com", "password": "correct-horse-battery"},
        )
        yield client, uuid.UUID(signup.json()["id"])


async def test_confirming_a_draft_sends_attachments_in_the_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _signed_up_client() as (client, user_id):
        async with async_session_factory() as db:
            upload = Upload(
                user_id=user_id, kind="document", filename="NOC.pdf",
                content_type="application/pdf", size_bytes=11, sha256=uuid.uuid4().hex * 2,
                data=b"%PDF-1.4 ok",
            )  # fmt: skip
            db.add(upload)
            db.add(
                Connector(
                    user_id=user_id, provider="gmail", access_token_encrypted=encrypt_token("t")
                )
            )
            await db.flush()
            action = PendingAction(
                user_id=user_id,
                tool_name="draft_email",
                status="pending",
                payload={
                    "to": "aditya@example.com",
                    "cc": ["neha@example.com"],
                    "subject": "Re: Internship documents",
                    "body": "Attached.",
                    "reply_to": {"thread_id": "thread-m1", "in_reply_to": "<m1@x>"},
                    "attachments": [
                        {"source": "upload", "upload_id": str(upload.id), "filename": "NOC.pdf",
                         "mime_type": "application/pdf", "size": 11},
                        {"source": "gmail", "message_id": "m1", "part_id": "1",
                         "filename": "offer.pdf", "mime_type": "application/pdf", "size": 9},
                    ],
                },
            )  # fmt: skip
            db.add(action)
            await db.commit()
            action_id = action.id

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "POST":
                return _json({"id": "sent-1", "threadId": "thread-m1"})
            if path.endswith("/attachments/fresh-id"):
                return _json({"data": _b64("%PDF offer")})
            # attachmentIds change on every read, so the send must use this fresh one.
            return _json(
                _message("m1", plain="x", attachment=("offer.pdf", "application/pdf", "fresh-id"))
            )

        requests = _mock_gmail(monkeypatch, handler)
        response = await client.post(f"/api/v1/chat/actions/{action_id}/confirm")

    assert response.status_code == 200
    assert response.json()["status"] == "executed"
    send = requests[-1]
    assert send.url.params["uploadType"] == "multipart"
    boundary = send.headers["content-type"].split("boundary=")[1].encode()
    _, metadata_part, rfc822_part, closing = send.content.split(b"--" + boundary)
    assert closing == b"--\r\n"
    assert json.loads(metadata_part.split(b"\r\n\r\n", 1)[1]) == {"threadId": "thread-m1"}
    sent = email.message_from_bytes(rfc822_part.split(b"\r\n\r\n", 1)[1], policy=policy.default)
    assert sent["Cc"] == "neha@example.com"
    assert sent["In-Reply-To"] == "<m1@x>"
    files = {a.get_filename(): a.get_content() for a in sent.iter_attachments()}  # type: ignore[attr-defined]
    assert files == {"NOC.pdf": b"%PDF-1.4 ok", "offer.pdf": b"%PDF offer"}


async def test_prepare_draft_rejects_template_placeholders() -> None:
    user_id, _ = await _user_with_mail()
    async with async_session_factory() as db:
        with pytest.raises(ValueError, match=r"\[Your Name\]"):
            await email_drafts.prepare_draft(
                {"to": "a@example.com", "body": "Thanks!\n\nBest regards,\n[Your Name]"},
                user_id,
                db,
            )
        # Ordinary brackets in an email are fine.
        draft = await email_drafts.prepare_draft(
            {"to": "a@example.com", "body": "See section [2] of the form."}, user_id, db
        )
        assert draft["body"] == "See section [2] of the form."
