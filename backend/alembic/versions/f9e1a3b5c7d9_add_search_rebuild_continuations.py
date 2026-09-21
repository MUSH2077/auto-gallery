"""Persist bounded search rebuild phases and write cooldowns.

Revision ID: f9e1a3b5c7d9
Revises: f8d0e2a4b6c8
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f9e1a3b5c7d9"
down_revision = "f8d0e2a4b6c8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "search_rebuilds",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("phase", sa.String(24), nullable=False),
        sa.Column("owner", sa.String(64)),
        sa.Column("progress", postgresql.JSONB(), nullable=False),
        sa.Column("last_error", sa.Text()),
    )
    op.create_index("uq_search_rebuild_active", "search_rebuilds", [sa.text("(true)")], unique=True,
                    postgresql_where=sa.text("state NOT IN ('complete', 'failed')"))
    op.create_table("search_rebuild_replay",
        sa.Column("build_id", sa.Uuid(), primary_key=True),
        sa.Column("outbox_id", sa.Uuid(), primary_key=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
    )
    op.add_column("search_delivery_receipts", sa.Column("rebuild_id", sa.Uuid()))
    op.add_column("search_delivery_receipts", sa.Column("continuation", postgresql.JSONB()))
    op.add_column("search_delivery_receipts", sa.Column("write_available_at", sa.DateTime(timezone=True)))
    op.create_index("ix_search_delivery_write_available", "search_delivery_receipts", ["write_available_at"])


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM search_rebuilds WHERE state NOT IN ('complete', 'failed'))
            OR EXISTS (SELECT 1 FROM search_delivery_receipts WHERE state NOT IN ('complete', 'failed')) THEN
            RAISE EXCEPTION 'Drain or reconcile active search rebuilds and receipts before downgrade';
        END IF;
    END $$""")
    op.drop_index("ix_search_delivery_write_available", table_name="search_delivery_receipts")
    op.drop_column("search_delivery_receipts", "write_available_at")
    op.drop_column("search_delivery_receipts", "continuation")
    op.drop_column("search_delivery_receipts", "rebuild_id")
    op.drop_table("search_rebuild_replay")
    op.drop_table("search_rebuilds")
