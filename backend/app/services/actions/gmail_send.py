import json
import uuid
from dataclasses import dataclass
from email.message import EmailMessage

from app.services.ingestion import gmail as gmail_api

# The upload endpoint takes messages up to 35 MB; the plain JSON one tops out far lower,
# which a couple of PDFs would exceed.
_SEND_ENDPOINT = "https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send"


@dataclass
class Attachment:
    filename: str
    mime_type: str
    data: bytes


def build_message(
    to: str,
    subject: str,
    body: str,
    *,
    cc: list[str] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: list[Attachment] | None = None,
) -> bytes:
    message = EmailMessage()
    message["To"] = to
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    # Without these the recipient's mail app shows the reply as a brand-new conversation.
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
    message.set_content(body)
    for attachment in attachments or []:
        maintype, _, subtype = attachment.mime_type.partition("/")
        if not maintype or not subtype:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(
            attachment.data, maintype=maintype, subtype=subtype, filename=attachment.filename
        )
    # RFC 5322 line endings; some receiving servers reject bare LFs.
    return message.as_bytes(policy=message.policy.clone(linesep="\r\n"))


def _multipart_body(metadata: dict[str, str], raw: bytes) -> tuple[bytes, str]:
    """Google's multipart upload: a JSON metadata part (the threadId) then the RFC 822 bytes."""
    boundary = f"dt-{uuid.uuid4().hex}"
    body = (
        (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\nContent-Type: message/rfc822\r\n\r\n"
        ).encode()
        + raw
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/related; boundary={boundary}"


async def send_email(
    access_token: str,
    to: str,
    subject: str,
    body: str,
    *,
    cc: list[str] | None = None,
    thread_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: list[Attachment] | None = None,
) -> dict[str, object]:
    raw = build_message(
        to,
        subject,
        body,
        cc=cc,
        in_reply_to=in_reply_to,
        references=references,
        attachments=attachments,
    )
    # threadId files the sent reply into the original conversation in the user's own Gmail.
    content, content_type = _multipart_body({"threadId": thread_id} if thread_id else {}, raw)

    async with gmail_api.http_client() as client:
        response = await client.post(
            _SEND_ENDPOINT,
            params={"uploadType": "multipart"},
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": content_type},
            content=content,
        )
        response.raise_for_status()
        return dict(response.json())
