from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class NamingTemplate(TimestampMixin, Base):
    __tablename__ = "naming_templates"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str | None] = mapped_column(String(50))
    template: Mapped[str] = mapped_column(String(2000), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
