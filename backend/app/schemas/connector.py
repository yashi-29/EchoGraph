import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ConnectorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    external_account_email: str | None
    scopes: str
    token_expires_at: datetime | None
    created_at: datetime
