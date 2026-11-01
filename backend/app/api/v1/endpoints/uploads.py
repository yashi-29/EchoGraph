import uuid
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.db.session import get_db
from app.models.memory_item import MemoryItem
from app.models.upload import Upload
from app.models.user import User
from app.schemas.upload import UploadRead
from app.services.ingestion.uploads import UploadRejected, ingest_upload

router = APIRouter()
settings = get_settings()


def _read(upload: Upload, items: int) -> UploadRead:
    return UploadRead(
        id=upload.id,
        kind=upload.kind,
        filename=upload.filename,
        content_type=upload.content_type,
        size_bytes=upload.size_bytes,
        items=items,
        details=upload.details or {},
        created_at=upload.created_at,
    )


@router.post("/", response_model=UploadRead, status_code=status.HTTP_201_CREATED)
async def create_upload(
    file: UploadFile = File(...),
    # The file's own last-modified time from the browser, used as the memory's date when
    # the file carries none itself (PDF metadata and bookmark dates take precedence).
    occurred_at: datetime | None = Form(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UploadRead:
    """Parses and indexes the file within the request, so the response reflects the final
    state — there is no "processing" status to poll.
    """
    # Read one byte past the largest limit so an oversized file is rejected without
    # buffering all of it; ingest_upload applies the per-kind limit.
    cap = max(settings.upload_max_bytes, settings.upload_audio_max_bytes)
    data = await file.read(cap + 1)
    try:
        upload, count = await ingest_upload(
            current_user.id,
            file.filename or "upload",
            file.content_type,
            data,
            db,
            occurred_at=occurred_at,
        )
    except UploadRejected as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    return _read(upload, count)


@router.get("/", response_model=list[UploadRead])
async def list_uploads(
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[UploadRead]:
    # Counted live rather than stored: a re-imported bookmark export takes over the
    # bookmarks an older export shared with it.
    item_count = (
        select(func.count(MemoryItem.id))
        .where(MemoryItem.upload_id == Upload.id)
        .correlate(Upload)
        .scalar_subquery()
    )
    rows = await db.execute(
        select(Upload, item_count)
        .where(Upload.user_id == current_user.id)
        .order_by(Upload.created_at.desc())
    )
    return [_read(upload, count) for upload, count in rows]


async def _owned_upload(
    upload_id: uuid.UUID, user: User, db: AsyncSession, *, with_data: bool = False
) -> Upload:
    stmt = select(Upload).where(Upload.id == upload_id, Upload.user_id == user.id)
    if with_data:
        stmt = stmt.options(undefer(Upload.data))
    upload = await db.scalar(stmt)
    if upload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Upload not found")
    return upload


@router.get("/{upload_id}/file")
async def download_upload(
    upload_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    upload = await _owned_upload(upload_id, current_user, db, with_data=True)
    return Response(
        content=upload.data,
        # Served as a download, never inline — an uploaded .html must not render on our origin.
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(upload.filename)}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/{upload_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_upload(
    upload_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Deletes the file and every memory item indexed from it."""
    upload = await _owned_upload(upload_id, current_user, db)
    await db.execute(delete(MemoryItem).where(MemoryItem.upload_id == upload.id))
    await db.execute(delete(Upload).where(Upload.id == upload.id))
    await db.commit()
