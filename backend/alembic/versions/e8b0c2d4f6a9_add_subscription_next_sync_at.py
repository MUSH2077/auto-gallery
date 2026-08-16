"""add persistent subscription-source scheduler due time

Revision ID: e8b0c2d4f6a9
Revises: e7a9c1d3f5b8
Create Date: 2026-08-09
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e8b0c2d4f6a9"
down_revision: Union[str, None] = "e7a9c1d3f5b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEX_NAME = "ix_subscription_sources_next_sync_due"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE subscription_sources "
        "ADD COLUMN IF NOT EXISTS next_sync_at TIMESTAMPTZ"
    )

    # Keep the existing table writable while the ordered due index is built.
    # An interrupted CREATE INDEX CONCURRENTLY leaves an invalid catalog row;
    # remove only that remnant before retrying the idempotent build.
    with op.get_context().autocommit_block():
        valid = op.get_bind().execute(
            sa.text(
                """
                SELECT idx.indisvalid
                FROM pg_index AS idx
                JOIN pg_class AS relation ON relation.oid = idx.indexrelid
                JOIN pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE relation.relname = :index_name
                  AND namespace.nspname = current_schema()
                """
            ),
            {"index_name": INDEX_NAME},
        ).scalar_one_or_none()
        if valid is False:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
            "ON subscription_sources (next_sync_at ASC NULLS FIRST, id ASC) "
            "WHERE is_enabled IS TRUE"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
    op.execute(
        "ALTER TABLE subscription_sources "
        "DROP COLUMN IF EXISTS next_sync_at"
    )
