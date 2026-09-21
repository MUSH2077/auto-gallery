"""Persist remote search writes independently of worker execution leases.

Revision ID: f8d0e2a4b6c8
Revises: d0f2a4c6e8b1
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f8d0e2a4b6c8"
down_revision = "d0f2a4c6e8b1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "search_delivery_receipts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("index_uid", sa.String(128), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("versions", postgresql.JSONB(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("phase", sa.String(24), nullable=False),
        sa.Column("task_uid", sa.BigInteger()),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("poll_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text()),
    )
    op.create_index("uq_search_delivery_active", "search_delivery_receipts", [sa.text("(true)")],
                    unique=True, postgresql_where=sa.text("state NOT IN ('complete', 'failed')"))


def downgrade():
    # Refuse to discard the only authoritative remote identity during rollback.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM search_delivery_receipts WHERE state NOT IN ('complete', 'failed')) THEN
            RAISE EXCEPTION 'Drain or reconcile active search delivery receipts before downgrade';
        END IF;
    END $$""")
    op.drop_table("search_delivery_receipts")
