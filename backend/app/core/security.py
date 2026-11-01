from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import get_settings

settings = get_settings()

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    return _pwd_context.verify(password, hashed_password)


def create_access_token(subject: str) -> str:
    expires_delta = timedelta(minutes=settings.access_token_expire_minutes)
    expires_at = datetime.now(UTC) + expires_delta
    return jwt.encode(
        {"sub": subject, "exp": expires_at}, settings.secret_key, algorithm=_ALGORITHM
    )


def decode_access_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[_ALGORITHM])
    except JWTError:
        return None
    subject = payload.get("sub")
    return subject if isinstance(subject, str) else None


def create_oauth_state_token(provider: str) -> str:
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    payload = {"provider": provider, "purpose": "oauth_state", "exp": expires_at}
    return jwt.encode(payload, settings.secret_key, algorithm=_ALGORITHM)


def verify_oauth_state_token(token: str, provider: str) -> bool:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[_ALGORITHM])
    except JWTError:
        return False
    return payload.get("purpose") == "oauth_state" and payload.get("provider") == provider
