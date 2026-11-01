import base64
import hashlib

from cryptography.fernet import Fernet

from app.core.config import get_settings

settings = get_settings()


def _fernet() -> Fernet:
    # Derives a valid 32-byte Fernet key from SECRET_KEY so connector tokens
    # are encrypted at rest without needing a second secret to generate/rotate.
    digest = hashlib.sha256(settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_token(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()
