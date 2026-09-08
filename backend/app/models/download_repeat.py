"""Actor-scoped repeat intents survive operational job/task compaction."""

from uuid import UUID
from sqlalchemy import Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class DownloadRepeatIntent(TimestampMixin, Base):
    __tablename__ = "download_repeat_intents"
    __table_args__ = (UniqueConstraint("actor_user_id", "request_id", name="uq_download_repeat_actor_request"),)
    actor_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[UUID] = mapped_column(nullable=False)
    previous_job_id: Mapped[UUID] = mapped_column(nullable=False)
    previous_task_id: Mapped[UUID] = mapped_column(nullable=False)
    download_job_id: Mapped[UUID] = mapped_column(nullable=False)
    task_id: Mapped[UUID] = mapped_column(nullable=False)
