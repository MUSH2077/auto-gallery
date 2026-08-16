"""bind import and artifact leases to concrete executions

Revision ID: e7a9c1d3f5b8
Revises: d9e1f3a5b7c2
Create Date: 2026-08-09
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7a9c1d3f5b8"
down_revision: Union[str, None] = "d9e1f3a5b7c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE import_jobs "
        "ADD COLUMN IF NOT EXISTS execution_token UUID"
    )
    op.execute(
        "ALTER TABLE import_jobs "
        "ADD COLUMN IF NOT EXISTS execution_attempt INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE storage_artifacts "
        "ADD COLUMN IF NOT EXISTS lease_token UUID"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_import_jobs_execution_token "
        "ON import_jobs (execution_token)"
    )
    # storage_artifacts is already hundreds of thousands of rows in production.
    # Build outside Alembic's transaction and remove only an invalid remnant
    # left by an interrupted prior CONCURRENTLY attempt.
    with op.get_context().autocommit_block():
        valid = op.get_bind().execute(
            sa.text(
                """
                SELECT idx.indisvalid
                FROM pg_index AS idx
                JOIN pg_class AS relation ON relation.oid = idx.indexrelid
                JOIN pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE relation.relname = 'ix_storage_artifacts_lease_token'
                  AND namespace.nspname = current_schema()
                """
            )
        ).scalar_one_or_none()
        if valid is False:
            op.execute(
                "DROP INDEX CONCURRENTLY IF EXISTS "
                "ix_storage_artifacts_lease_token"
            )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_storage_artifacts_lease_token "
            "ON storage_artifacts (lease_token) WHERE lease_token IS NOT NULL"
        )
    op.execute(
        "ALTER TABLE asset_dedup_outbox "
        "DROP CONSTRAINT IF EXISTS ck_asset_dedup_outbox_event_type"
    )
    op.execute(
        "ALTER TABLE asset_dedup_outbox "
        "ADD CONSTRAINT ck_asset_dedup_outbox_event_type "
        "CHECK (event_type IN ('observe', 'hardlink', 'quarantine', 'purge'))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE asset_dedup_outbox "
        "DROP CONSTRAINT IF EXISTS ck_asset_dedup_outbox_event_type"
    )
    # Existing observe intents are retained for audit/recovery.  PostgreSQL
    # still enforces this restored three-value predicate for all new writes;
    # NOT VALID only avoids destructive cleanup during a rollback.
    op.execute(
        "ALTER TABLE asset_dedup_outbox "
        "ADD CONSTRAINT ck_asset_dedup_outbox_event_type "
        "CHECK (event_type IN ('hardlink', 'quarantine', 'purge')) NOT VALID"
    )
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS ix_storage_artifacts_lease_token"
        )
    op.execute("DROP INDEX IF EXISTS uq_import_jobs_execution_token")
    op.execute("ALTER TABLE storage_artifacts DROP COLUMN IF EXISTS lease_token")
    op.execute("ALTER TABLE import_jobs DROP COLUMN IF EXISTS execution_attempt")
    op.execute("ALTER TABLE import_jobs DROP COLUMN IF EXISTS execution_token")
