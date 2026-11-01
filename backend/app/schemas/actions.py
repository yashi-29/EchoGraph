import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class PendingActionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tool_name: str
    payload: dict[str, Any]
    status: str
    result: dict[str, Any] | None
    created_at: datetime
