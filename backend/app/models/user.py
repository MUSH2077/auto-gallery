import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    permissions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    preferences: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    nsfw_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    upload_quota_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    upload_used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
