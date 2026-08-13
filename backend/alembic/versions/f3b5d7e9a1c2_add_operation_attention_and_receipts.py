"""add operation attention state and repository sync receipts

Revision ID: f3b5d7e9a1c2
Revises: f2a4c6e8b0d1
Create Date: 2026-08-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f3b5d7e9a1c2"
down_revision: Union[str, None] = "f2a4c6e8b0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "task_runs",
        sa.Column("attention_state", sa.String(length=20), nullable=False, server_default="none"),
    )
    op.add_column("task_runs", sa.Column("reason_code", sa.String(length=80), nullable=True))
    op.add_column("task_runs", sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("task_runs", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("task_runs", sa.Column("compactable_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_task_runs_attention_updated",
        "task_runs",
        ["attention_state", "updated_at", "id"],
    )
    op.create_index(
        "ix_task_runs_compactable",
        "task_runs",
        ["compactable_at", "id"],
        postgresql_where=sa.text("compactable_at IS NOT NULL"),
    )

    op.create_table(
        "repository_sync_receipts",
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("source_download_job_id", sa.Uuid(), nullable=False),
        sa.Column("source_import_job_id", sa.Uuid(), nullable=True),
        sa.Column("source_task_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("outcome_code", sa.String(length=30), nullable=True),
        sa.Column("metadata_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("media_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("works_imported", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_excerpt", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("recovered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("recovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["subscription_sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_download_job_id", name="uq_repository_sync_receipts_download_job"),
    )
    op.create_index(
        "ix_repository_sync_receipts_repository_finished",
        "repository_sync_receipts",
        ["repository_id", "finished_at", "id"],
    )
    op.create_index(
        "ix_repository_sync_receipts_status_finished",
        "repository_sync_receipts",
        ["status", "finished_at"],
    )

    op.create_table(
        "search_index_states",
        sa.Column("index_uid", sa.String(length=128), nullable=False),
        sa.Column("database_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("indexed_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("database_document_count", sa.BigInteger(), nullable=True),
        sa.Column("index_document_count", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="catching_up"),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("index_uid", name="uq_search_index_states_uid"),
    )
    op.create_table(
        "maintenance_audit_events",
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_maintenance_audit_events_key"),
    )
    op.create_index(
        "ix_maintenance_audit_events_type_created",
        "maintenance_audit_events",
        ["event_type", "created_at"],
    )
    op.create_index(
        "ix_search_projection_outbox_pending_index",
        "search_projection_outbox",
        ["index_uid"],
        postgresql_where=sa.text("completed_at IS NULL"),
    )

    # Backfill a compact receipt before any later maintenance compacts the
    # operational rows. gen_random_uuid() is provided by PostgreSQL 16.
    op.execute(
        """
        INSERT INTO repository_sync_receipts (
            id, repository_id, source_download_job_id, source_import_job_id,
            source_task_id, source, status, outcome_code, metadata_count,
            media_count, works_imported, attempts, error_code, error_excerpt, started_at,
            finished_at, duration_ms, detail, created_at, updated_at
        )
        SELECT
            gen_random_uuid(), dj.subscription_source_id, dj.id,
            latest_import.id, download_task.id, dj.source, dj.status,
            dj.manifest #>> '{outcome,code}',
            CASE WHEN (dj.manifest #>> '{outcome,metadata_count}') ~ '^[0-9]+$'
              THEN (dj.manifest #>> '{outcome,metadata_count}')::integer ELSE 0 END,
            CASE WHEN (dj.manifest #>> '{outcome,media_count}') ~ '^[0-9]+$'
              THEN (dj.manifest #>> '{outcome,media_count}')::integer ELSE 0 END,
            CASE WHEN (dj.manifest #>> '{import_stats,works}') ~ '^[0-9]+$'
              THEN (dj.manifest #>> '{import_stats,works}')::integer ELSE 0 END,
            COALESCE(dj.retry_count, 0),
            CASE
              WHEN dj.status = 'stale' THEN 'lost_heartbeat'
              WHEN LOWER(COALESCE(dj.error_log, '')) LIKE '%out of memory%'
                OR LOWER(COALESCE(dj.error_log, '')) LIKE '%oom%' THEN 'out_of_memory'
              WHEN dj.status = 'failed' THEN 'task_failed'
              ELSE NULL
            END,
            LEFT(dj.error_log, 500),
            COALESCE(download_task.started_at, dj.created_at),
            COALESCE(download_task.finished_at, dj.updated_at, dj.created_at),
            CASE
              WHEN COALESCE(download_task.started_at, dj.created_at) IS NULL THEN NULL
              ELSE (EXTRACT(EPOCH FROM (
                COALESCE(download_task.finished_at, dj.updated_at, dj.created_at)
                - COALESCE(download_task.started_at, dj.created_at)
              )) * 1000)::bigint
            END,
            jsonb_build_object('backfilled', true), now(), now()
        FROM download_jobs dj
        LEFT JOIN LATERAL (
            SELECT ij.id FROM import_jobs ij
            WHERE ij.download_job_id = dj.id
            ORDER BY ij.created_at DESC, ij.id DESC LIMIT 1
        ) latest_import ON true
        LEFT JOIN task_runs download_task
          ON download_task.subject_type = 'download_job'
         AND download_task.subject_id = dj.id
        WHERE dj.subscription_source_id IS NOT NULL
          AND dj.status IN ('complete', 'failed', 'stale', 'cancelled')
        ON CONFLICT (source_download_job_id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE task_runs
        SET attention_state = CASE
              WHEN status IN ('failed', 'stale') THEN 'open'
              ELSE 'none'
            END,
            reason_code = CASE
              WHEN status = 'failed' THEN 'task_failed'
              WHEN status = 'stale' THEN 'lost_heartbeat'
              ELSE NULL
            END,
            compactable_at = CASE WHEN status = 'complete' THEN now() ELSE NULL END
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_search_projection_outbox_pending_index",
        table_name="search_projection_outbox",
    )
    op.drop_table("search_index_states")
    op.drop_index(
        "ix_maintenance_audit_events_type_created",
        table_name="maintenance_audit_events",
    )
    op.drop_table("maintenance_audit_events")
    op.drop_index("ix_repository_sync_receipts_status_finished", table_name="repository_sync_receipts")
    op.drop_index("ix_repository_sync_receipts_repository_finished", table_name="repository_sync_receipts")
    op.drop_table("repository_sync_receipts")
    op.drop_index("ix_task_runs_compactable", table_name="task_runs")
    op.drop_index("ix_task_runs_attention_updated", table_name="task_runs")
    op.drop_column("task_runs", "compactable_at")
    op.drop_column("task_runs", "resolved_at")
    op.drop_column("task_runs", "acknowledged_at")
    op.drop_column("task_runs", "reason_code")
    op.drop_column("task_runs", "attention_state")
