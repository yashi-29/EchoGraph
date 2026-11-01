from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import LoginRequest, UserCreate, UserRead

router = APIRouter()
settings = get_settings()


def _issue_session(response: Response, user_id: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=create_access_token(user_id),
        max_age=settings.access_token_expire_minutes * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )


@router.post("/signup", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def signup(
    payload: UserCreate, response: Response, db: AsyncSession = Depends(get_db)
) -> User:
    user = User(
        email=payload.email.lower(),
        hashed_password=hash_password(payload.password),
        full_name=(payload.full_name or "").strip() or None,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "An account with that email already exists"
        ) from exc
    await db.refresh(user)
    _issue_session(response, str(user.id))
    return user


@router.post("/login", response_model=UserRead)
async def login(
    payload: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)
) -> User:
    result = await db.execute(select(User).where(User.email == payload.email.lower()))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is disabled")
    _issue_session(response, str(user.id))
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    response.delete_cookie(settings.session_cookie_name)


@router.get("/me", response_model=UserRead)
async def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user
