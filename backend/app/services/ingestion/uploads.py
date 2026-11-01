import asyncio
import hashlib
import uuid
from datetime import datetime
from pathlib import PurePath
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from voyageai.error import VoyageError

from app.core.config import get_settings
from app.models.upload import Upload
from app.services.ingestion import bookmarks as bookmark_parsing
from app.services.ingestion import documents, images, voice_notes
from app.services.ingestion.base import NormalizedItem
from app.services.ingestion.sync import store_items

settings = get_settings()

# A 400-page PDF is ~200 chunks; beyond that only the start is indexed (and flagged).
_MAX_CHUNKS = 200
_TEXT_EXTENSIONS = {".txt", ".md", ".markdown"}
# Saved-from-the-web images often arrive with no extension ("download", "image").
_IMAGE_SIGNATURES = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")


class UploadRejected(Exception):
    """The upload can't be indexed; `detail` is safe to show the user."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def detect_kind(filename: str, data: bytes) -> tuple[str, str]:
    """(kind, format) from the file extension, with a content sniff for PDFs, common images
    and bookmark exports — browsers report unreliable content types for .md, .m4a and friends.
    """
    extension = PurePath(filename).suffix.lower()
    if extension == ".pdf" or data.startswith(b"%PDF-"):
        return "document", "pdf"
    if extension == ".svg":
        return "image", "svg"
    if extension in images.IMAGE_EXTENSIONS or data.startswith(_IMAGE_SIGNATURES):
        return "image", "raster"
    if extension == ".docx":
        return "document", "docx"
    if extension in _TEXT_EXTENSIONS:
        return "document", "text"
    if extension in {".html", ".htm"}:
        if bookmark_parsing.NETSCAPE_MARKER in data[:2048]:
            return "bookmark", "netscape"
        raise UploadRejected(
            415,
            "HTML files are only supported as browser bookmark exports "
            "(Bookmarks → Export in Chrome, Edge, Firefox or Safari).",
        )
    if extension in voice_notes.AUDIO_EXTENSIONS:
        return "voice_note", "audio"
    raise UploadRejected(
        415,
        "Unsupported file type. Upload a PDF, Word (.docx) file, .txt/.md note, image "
        "(jpg, png, heic, webp, gif, tiff, svg, …), voice note (mp3, m4a, wav, webm, ogg, "
        "flac) or a browser bookmark export (.html).",
    )


def max_bytes_for(kind: str) -> int:
    return settings.upload_audio_max_bytes if kind == "voice_note" else settings.upload_max_bytes


def _chunk_items(
    upload_id: uuid.UUID,
    filename: str,
    text: str,
    occurred_at: datetime | None,
    extra_metadata: dict[str, Any],
) -> tuple[list[NormalizedItem], bool]:
    chunks = documents.chunk_text(text)
    truncated = len(chunks) > _MAX_CHUNKS
    chunks = chunks[:_MAX_CHUNKS]
    items = []
    for index, chunk in enumerate(chunks):
        # Title line first, like every other source — the timeline and chat both read it.
        title = filename if len(chunks) == 1 else f"{filename} (part {index + 1} of {len(chunks)})"
        items.append(
            NormalizedItem(
                external_id=f"{upload_id}:{index}",
                content=f"{title}\n\n{chunk}",
                item_metadata={
                    "filename": filename,
                    "chunk_index": index,
                    "chunk_count": len(chunks),
                    **extra_metadata,
                },
                occurred_at=occurred_at,
            )
        )
    return items, truncated


def _bookmark_items(filename: str, data: bytes) -> list[NormalizedItem]:
    html = data.decode("utf-8", errors="replace")
    items = []
    for bookmark in bookmark_parsing.parse_bookmarks(html):
        body = bookmark.url + (f"\nFolder: {bookmark.folder}" if bookmark.folder else "")
        items.append(
            NormalizedItem(
                # Keyed by URL, so re-importing a newer export updates instead of duplicating.
                external_id=hashlib.sha256(bookmark.url.encode()).hexdigest(),
                content=f"{bookmark.title}\n\n{body}",
                item_metadata={
                    "url": bookmark.url,
                    "folder": bookmark.folder,
                    "filename": filename,
                },
                occurred_at=bookmark.added_at,
            )
        )
    return items


async def _items_for(
    upload: Upload, fmt: str, data: bytes, occurred_at: datetime | None
) -> list[NormalizedItem]:
    """Turns the file into memory items, recording what was learned about it on
    `upload.details`.
    """
    if fmt == "netscape":
        items = _bookmark_items(upload.filename, data)
        upload.details = {"bookmarks": len(items)}
        return items

    if fmt == "audio":
        try:
            text = await voice_notes.transcribe(upload.filename, data)
        except voice_notes.TranscriptionFailed as exc:
            raise UploadRejected(502, "Couldn't transcribe this voice note right now.") from exc
        items, truncated = _chunk_items(upload.id, upload.filename, text, occurred_at, {})
        upload.details = {"transcript_chars": len(text), "truncated": truncated}
        return items

    if fmt in ("raster", "svg"):
        try:
            if fmt == "svg":
                image = images.extract_svg(data)
            else:
                # Pillow decoding and Tesseract are CPU-bound; keep the event loop free.
                image = await asyncio.to_thread(images.extract_image, data)
        except images.ImageUnreadable as exc:
            raise UploadRejected(422, str(exc)) from exc
        except images.OcrFailed as exc:
            raise UploadRejected(502, "Couldn't read the text in this image right now.") from exc
        facts = {
            "width": image.width,
            "height": image.height,
            "camera": image.camera,
            "pages": image.frames if image.frames > 1 else None,
        }
        extra = {key: value for key, value in facts.items() if value is not None}
        # EXIF capture time beats the file's last-modified time, which for a photo copied
        # off a phone is just when it was copied.
        items, truncated = _chunk_items(
            upload.id, upload.filename, images.describe(image), image.taken_at or occurred_at, extra
        )
        upload.details = {**extra, "ocr_chars": len(image.text), "truncated": truncated}
        return items

    try:
        if fmt == "pdf":
            # pypdf is pure-Python and CPU-bound; keep the event loop free while it parses.
            extracted = await asyncio.to_thread(documents.extract_pdf, data)
        elif fmt == "docx":
            extracted = await asyncio.to_thread(documents.extract_docx, data)
        else:
            extracted = documents.decode_text(data)
    except documents.DocumentUnreadable as exc:
        raise UploadRejected(422, str(exc)) from exc

    extra = {"pages": extracted.pages} if extracted.pages is not None else {}
    # The PDF's own creation date beats the file's last-modified time, which for a
    # downloaded certificate is just when it was downloaded.
    items, truncated = _chunk_items(
        upload.id, upload.filename, extracted.text, extracted.created_at or occurred_at, extra
    )
    upload.details = {**extra, "truncated": truncated}
    return items


async def ingest_upload(
    user_id: uuid.UUID,
    filename: str,
    content_type: str | None,
    data: bytes,
    db: AsyncSession,
    *,
    occurred_at: datetime | None = None,
) -> tuple[Upload, int]:
    """Parses, embeds and stores one uploaded file. Returns the upload and how many memory
    items it produced. Nothing is stored unless the whole file indexes successfully.
    """
    filename = PurePath(filename.replace("\\", "/")).name[:255] or "upload"
    if not data:
        raise UploadRejected(422, "This file is empty.")
    kind, fmt = detect_kind(filename, data)
    limit = max_bytes_for(kind)
    if len(data) > limit:
        raise UploadRejected(413, f"This file is too large — the limit is {limit // 2**20} MB.")

    sha256 = hashlib.sha256(data).hexdigest()
    duplicate = await db.scalar(
        select(Upload.filename).where(Upload.user_id == user_id, Upload.sha256 == sha256)
    )
    if duplicate is not None:
        raise UploadRejected(409, f"You've already uploaded this file (as {duplicate}).")

    upload = Upload(
        id=uuid.uuid4(),
        user_id=user_id,
        kind=kind,
        filename=filename,
        content_type=(content_type or "application/octet-stream")[:100],
        size_bytes=len(data),
        sha256=sha256,
        data=data,
        details={},
    )
    items = await _items_for(upload, fmt, data, occurred_at)
    if not items:
        raise UploadRejected(
            422,
            "No bookmarks found in this file."
            if kind == "bookmark"
            else "No text found in this file — scanned PDFs (images of text) aren't supported yet.",
        )

    db.add(upload)
    try:
        await db.flush()
    except IntegrityError as exc:  # the same file uploaded concurrently
        await db.rollback()
        raise UploadRejected(409, "You've already uploaded this file.") from exc
    try:
        count = await store_items(user_id, kind, items, db, upload_id=upload.id)
    except VoyageError as exc:
        await db.rollback()
        raise UploadRejected(
            502, "Couldn't index this file right now — try again in a minute."
        ) from exc
    except Exception:
        await db.rollback()
        raise
    await db.refresh(upload, ["created_at"])
    return upload, count
