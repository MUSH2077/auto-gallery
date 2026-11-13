"""PostgreSQL search catalog, physical index generations and completed commands."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Identity, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base


class PostgresSearchIndex(Base):
    __tablename__ = "search_pg_indexes"
    uid: Mapped[str] = mapped_column(Text, primary_key=True)
    storage_id: Mapped[UUID] = mapped_column(nullable=False)
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class PostgresSearchDocument(Base):
    __tablename__ = "search_pg_documents"
    __table_args__ = (
        Index("ix_search_pg_documents_vector", "search_vector", postgresql_using="gin"),
        Index("ix_search_pg_documents_tokens", "tokens", postgresql_using="gin"),
        Index("ix_search_pg_documents_text", "search_text", postgresql_using="gin", postgresql_ops={"search_text": "gin_trgm_ops"}),
    )
    storage_id: Mapped[UUID] = mapped_column(primary_key=True)
    entity_id: Mapped[str] = mapped_column(Text, primary_key=True)
    document: Mapped[dict] = mapped_column(JSONB, nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    tokens: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"))
    search_vector: Mapped[str] = mapped_column(TSVECTOR, nullable=False)


class PostgresSearchTask(Base):
    __tablename__ = "search_pg_tasks"
    uid: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    index_uid: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24))
    enqueued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    details: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[dict | None] = mapped_column(JSONB)
