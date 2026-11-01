import uuid

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.user import User

settings = get_settings()


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    not_authenticated = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise not_authenticated

    subject = decode_access_token(token)
    if subject is None:
        raise not_authenticated

    try:
        user_id = uuid.UUID(subject)
    except ValueError:
        raise not_authenticated from None

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise not_authenticated
    return user
