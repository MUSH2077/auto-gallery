from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class TagRead(BaseModel):
    id: UUID
    normalized_name: str
    category: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
