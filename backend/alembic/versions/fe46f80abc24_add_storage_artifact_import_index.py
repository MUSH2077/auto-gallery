"""add storage artifact import-job lookup index

Revision ID: fe46f80abc24
Revises: fd35e7f9ab13
Create Date: 2026-09-21
"""

from alembic import op
import sqlalchemy as sa


revision: str = "fe46f80abc24"
down_revision: str = "fd35e7f9ab13"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


INDEX_NAME = "ix_storage_artifacts_import_job_id"


def upgrade() -> None:
    # Import deletion updates the referencing artifact rows.  Build the index
    # without blocking the live artifact ledger, and repair an invalid remnant
    # left by an interrupted concurrent build before retrying.
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
            "ON storage_artifacts (import_job_id)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
