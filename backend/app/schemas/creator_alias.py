from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class CreatorAliasRead(BaseModel):
    id: UUID
    creator_id: UUID
    value: str
    normalized_value: str
    source: str
    kind: str
    is_current: bool
    first_seen_at: datetime
    last_seen_at: datetime
    source_ref: str | None = None

    model_config = {"from_attributes": True}
