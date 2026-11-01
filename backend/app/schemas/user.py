import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

ResponseTone = Literal["balanced", "concise", "warm"]
MemoryWindowDays = Literal[90, 365] | None


class UserBase(BaseModel):
    email: EmailStr


class UserCreate(UserBase):
    password: str
    full_name: str | None = Field(None, max_length=100)


class LoginRequest(UserBase):
    password: str


class UserRead(UserBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str | None
    is_active: bool
    created_at: datetime
    allow_content_in_chat: bool
    response_tone: ResponseTone
    memory_window_days: MemoryWindowDays
    timezone: str | None


class ProfileUpdate(BaseModel):
    full_name: str | None = Field(None, max_length=100)
    email: EmailStr | None = None
    # Required only when changing the email address (it's the login identifier).
    current_password: str | None = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class PreferencesUpdate(BaseModel):
    """Only fields present in the request are changed (see `model_fields_set`), so an
    explicit `"memory_window_days": null` means "all time", while omitting it leaves it as is.
    """

    allow_content_in_chat: bool | None = None
    response_tone: ResponseTone | None = None
    memory_window_days: MemoryWindowDays = None


class AccountDeletion(BaseModel):
    password: str
