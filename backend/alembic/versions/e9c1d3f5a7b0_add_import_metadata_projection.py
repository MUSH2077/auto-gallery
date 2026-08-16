"""add durable import metadata projection state

Revision ID: e9c1d3f5a7b0
Revises: e8b0c2d4f6a9
Create Date: 2026-08-09
"""

from typing import Sequence, Union

from alembic import op


revision: str = "e9c1d3f5a7b0"
down_revision: Union[str, None] = "e8b0c2d4f6a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE import_curation_outbox
          ADD COLUMN IF NOT EXISTS metadata_path VARCHAR(2000),
          ADD COLUMN IF NOT EXISTS metadata_state VARCHAR(20)
            NOT NULL DEFAULT 'complete',
          ADD COLUMN IF NOT EXISTS metadata_attempts INTEGER
            NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS metadata_available_at TIMESTAMPTZ
            NOT NULL DEFAULT now(),
          ADD COLUMN IF NOT EXISTS metadata_lease_expires_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS metadata_lease_token UUID,
          ADD COLUMN IF NOT EXISTS metadata_completed_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS metadata_last_error TEXT
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conname = 'ck_import_curation_outbox_metadata_state'
              AND conrelid = 'import_curation_outbox'::regclass
          ) THEN
            ALTER TABLE import_curation_outbox
              ADD CONSTRAINT ck_import_curation_outbox_metadata_state
              CHECK (
                metadata_state IN ('pending', 'processing', 'complete', 'failed')
              ) NOT VALID;
          END IF;
        END $$
        """
    )
    op.execute(
        """
        ALTER TABLE import_curation_outbox
          VALIDATE CONSTRAINT ck_import_curation_outbox_metadata_state
        """
    )

    # d9 may already have accepted imports before this repair is deployed.
    # Re-arm only rows carrying a real import identity; the bounded outbox
    # worker reconstructs their target and closes any best-effort write gap.
    op.execute(
        """
        UPDATE import_curation_outbox
        SET metadata_state = 'pending',
            metadata_available_at = now(),
            metadata_completed_at = NULL,
            metadata_last_error = NULL
        WHERE source IS NOT NULL
          AND source_work_id IS NOT NULL
        """
    )
    op.execute(
        """
        ALTER TABLE import_curation_outbox
          ALTER COLUMN metadata_state SET DEFAULT 'pending'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_import_curation_outbox_metadata_ready
        ON import_curation_outbox (metadata_available_at, id)
        WHERE metadata_state IN ('pending', 'failed')
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_import_curation_outbox_metadata_lease
        ON import_curation_outbox (metadata_lease_expires_at, id)
        WHERE metadata_state = 'processing'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_import_curation_outbox_metadata_lease")
    op.execute("DROP INDEX IF EXISTS ix_import_curation_outbox_metadata_ready")
    op.execute(
        """
        ALTER TABLE import_curation_outbox
          DROP CONSTRAINT IF EXISTS ck_import_curation_outbox_metadata_state,
          DROP COLUMN IF EXISTS metadata_last_error,
          DROP COLUMN IF EXISTS metadata_completed_at,
          DROP COLUMN IF EXISTS metadata_lease_token,
          DROP COLUMN IF EXISTS metadata_lease_expires_at,
          DROP COLUMN IF EXISTS metadata_available_at,
          DROP COLUMN IF EXISTS metadata_attempts,
          DROP COLUMN IF EXISTS metadata_state,
          DROP COLUMN IF EXISTS metadata_path
        """
    )
