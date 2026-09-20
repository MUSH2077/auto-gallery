"""Reconcile purge bookkeeping for quota and media derivatives.

Revision ID: fd35e7f9ab13
Revises: fc24d6e8fa02
Create Date: 2026-09-20
"""

from typing import Sequence, Union

from alembic import op


revision: str = "fd35e7f9ab13"
down_revision: Union[str, None] = "fc24d6e8fa02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


RECONCILE_SQL = r"""
WITH imported_charges AS (
    SELECT
        COALESCE(
            CASE
                WHEN ws.raw_metadata->>'uploaded_by_user_id' ~ '^[1-9][0-9]*$'
                THEN (ws.raw_metadata->>'uploaded_by_user_id')::integer
            END,
            uploader.id
        ) AS user_id,
        SUM((file_entry->>'size')::bigint) AS charged_bytes
    FROM work_sources AS ws
    LEFT JOIN work_curation_states AS state
        ON state.work_id = ws.work_id
    LEFT JOIN users AS uploader
        ON uploader.username = ws.raw_metadata->>'uploaded_by'
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE
            WHEN jsonb_typeof(ws.raw_metadata->'files') = 'array'
            THEN ws.raw_metadata->'files'
            ELSE '[]'::jsonb
        END
    ) AS file_entry
    WHERE lower(ws.source) = 'manual'
      AND state.visibility IS DISTINCT FROM 'purged'
      AND file_entry->>'size' ~ '^[0-9]+$'
    GROUP BY 1
),
pending_charges AS (
    SELECT
        job.owner_user_id AS user_id,
        SUM(COALESCE(artifact.file_size, 0)) AS charged_bytes
    FROM download_jobs AS job
    JOIN storage_artifacts AS artifact
        ON artifact.download_job_id = job.id
    WHERE lower(job.source) = 'manual'
      AND job.owner_user_id IS NOT NULL
      AND artifact.artifact_type <> 'metadata_json'
      AND NOT EXISTS (
          SELECT 1
          FROM work_sources AS imported
          WHERE lower(imported.source) = 'manual'
            AND imported.source_work_id = regexp_replace(
                job.source_url,
                '^manual://',
                ''
            )
      )
    GROUP BY job.owner_user_id
),
charges AS (
    SELECT user_id, SUM(charged_bytes) AS charged_bytes
    FROM (
        SELECT user_id, charged_bytes FROM imported_charges
        UNION ALL
        SELECT user_id, charged_bytes FROM pending_charges
    ) AS all_charges
    WHERE user_id IS NOT NULL
    GROUP BY user_id
)
UPDATE users AS target
SET upload_used_bytes = COALESCE(charges.charged_bytes, 0)
FROM (
    SELECT
        target_user.id AS user_id,
        COALESCE(charges.charged_bytes, 0) AS charged_bytes
    FROM users AS target_user
    LEFT JOIN charges ON charges.user_id = target_user.id
) AS charges
WHERE target.id = charges.user_id
  AND target.upload_used_bytes IS DISTINCT FROM charges.charged_bytes
"""


def upgrade() -> None:
    op.drop_constraint(
        "ck_media_derivative_outbox_state",
        "media_derivative_outbox",
        type_="check",
    )
    op.create_check_constraint(
        "ck_media_derivative_outbox_state",
        "media_derivative_outbox",
        "state IN ('pending', 'processing', 'complete', 'failed', 'cancelled')",
    )
    op.execute(
        """
        UPDATE media_derivative_outbox AS derivative
        SET state = 'cancelled',
            completed_at = COALESCE(derivative.completed_at, now()),
            lease_expires_at = NULL,
            last_error = NULL
        FROM asset_storage_states AS storage
        WHERE storage.asset_id = derivative.asset_id
          AND storage.storage_state = 'purged'
          AND derivative.state <> 'complete'
        """
    )
    op.execute(RECONCILE_SQL)


def downgrade() -> None:
    op.execute(
        """
        UPDATE media_derivative_outbox
        SET state = 'failed',
            completed_at = NULL,
            last_error = COALESCE(last_error, 'Source asset was purged')
        WHERE state = 'cancelled'
        """
    )
    op.drop_constraint(
        "ck_media_derivative_outbox_state",
        "media_derivative_outbox",
        type_="check",
    )
    op.create_check_constraint(
        "ck_media_derivative_outbox_state",
        "media_derivative_outbox",
        "state IN ('pending', 'processing', 'complete', 'failed')",
    )
    # The stale pre-upgrade quota aggregate cannot be reconstructed safely.
