"""add unified task run tables

Revision ID: d3e4f5a6b7c8
Revises: a6c8e0f2b4d6
"""

from alembic import op


revision = "d3e4f5a6b7c8"
down_revision = "a6c8e0f2b4d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS task_runs (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            kind VARCHAR(50) NOT NULL,
            operation_type VARCHAR(80),
            subject_type VARCHAR(50),
            subject_id UUID,
            parent_task_id UUID REFERENCES task_runs(id) ON DELETE SET NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'enqueued',
            queue_name VARCHAR(80),
            rq_job_id VARCHAR(255),
            title VARCHAR(500),
            source VARCHAR(50),
            source_url VARCHAR(2000),
            progress_stage VARCHAR(50),
            progress_current INTEGER,
            progress_total INTEGER,
            progress_data JSONB,
            result_data JSONB,
            error_log TEXT,
            meta JSONB,
            priority INTEGER NOT NULL DEFAULT 10,
            attempts INTEGER NOT NULL DEFAULT 0,
            enqueued_at TIMESTAMPTZ,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            last_heartbeat_at TIMESTAMPTZ
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS task_events (
            id BIGSERIAL PRIMARY KEY,
            task_run_id UUID NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
            event_type VARCHAR(50) NOT NULL,
            from_status VARCHAR(20),
            to_status VARCHAR(20),
            message TEXT,
            payload JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_task_runs_kind_status_created ON task_runs (kind, status, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_task_runs_operation_type_created ON task_runs (operation_type, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_task_runs_subject ON task_runs (subject_type, subject_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_task_runs_parent ON task_runs (parent_task_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_task_events_task_created ON task_events (task_run_id, created_at)")
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_task_runs_subject
        ON task_runs (subject_type, subject_id)
        WHERE subject_type IS NOT NULL AND subject_id IS NOT NULL
    """)

    # Backfill domain jobs into the unified task surface. The UUID expression is
    # deterministic and extension-free, so repeated migrations/repairs converge.
    op.execute("""
        INSERT INTO task_runs (
            id, created_at, updated_at, kind, operation_type, subject_type, subject_id,
            status, queue_name, title, source, source_url, progress_stage,
            progress_data, error_log, priority, attempts, enqueued_at,
            started_at, finished_at, last_heartbeat_at, meta
        )
        SELECT
            (
              substr(md5('download:' || id::text), 1, 8) || '-' ||
              substr(md5('download:' || id::text), 9, 4) || '-' ||
              substr(md5('download:' || id::text), 13, 4) || '-' ||
              substr(md5('download:' || id::text), 17, 4) || '-' ||
              substr(md5('download:' || id::text), 21, 12)
            )::uuid,
            created_at, updated_at, 'download', 'download', 'download_job', id,
            CASE
                WHEN status IN ('pending', 'queued') THEN 'enqueued'
                WHEN status IN ('downloading', 'downloaded', 'importing') THEN 'running'
                ELSE status
            END,
            'downloads', 'Download ' || source, source, source_url, pipeline_stage,
            progress_data, error_log, priority, retry_count,
            CASE WHEN status IN ('enqueued', 'pending') THEN created_at ELSE NULL END,
            CASE WHEN status IN ('downloading', 'downloaded', 'importing') THEN updated_at ELSE NULL END,
            CASE WHEN status IN ('complete', 'failed', 'cancelled', 'stale') THEN updated_at ELSE NULL END,
            last_heartbeat_at,
            jsonb_build_object(
                'subscription_id', subscription_id,
                'subscription_source_id', subscription_source_id,
                'retry_count', retry_count
            )
        FROM download_jobs
        ON CONFLICT DO NOTHING
    """)
    op.execute("""
        INSERT INTO task_runs (
            id, created_at, updated_at, kind, operation_type, subject_type, subject_id,
            parent_task_id, status, queue_name, title, progress_stage, progress_current,
            progress_total, progress_data, error_log, priority, attempts, enqueued_at,
            started_at, finished_at, last_heartbeat_at, meta
        )
        SELECT
            (
              substr(md5('import:' || ij.id::text), 1, 8) || '-' ||
              substr(md5('import:' || ij.id::text), 9, 4) || '-' ||
              substr(md5('import:' || ij.id::text), 13, 4) || '-' ||
              substr(md5('import:' || ij.id::text), 17, 4) || '-' ||
              substr(md5('import:' || ij.id::text), 21, 12)
            )::uuid,
            ij.created_at, ij.updated_at, 'import', 'import', 'import_job', ij.id,
            tr.id,
            CASE WHEN ij.status IN ('pending', 'queued') THEN 'enqueued' ELSE ij.status END,
            'imports', 'Import metadata', ij.progress_stage, ij.progress_works_done,
            ij.progress_works_total, ij.progress_data, ij.error_log, ij.priority,
            ij.import_retry_count,
            CASE WHEN ij.status IN ('enqueued', 'pending') THEN ij.created_at ELSE NULL END,
            CASE WHEN ij.status = 'running' THEN ij.updated_at ELSE NULL END,
            CASE WHEN ij.status IN ('complete', 'failed', 'cancelled', 'stale') THEN ij.updated_at ELSE NULL END,
            ij.last_heartbeat_at,
            jsonb_build_object('download_job_id', ij.download_job_id, 'max_import_retries', ij.max_import_retries)
        FROM import_jobs ij
        LEFT JOIN task_runs tr ON tr.subject_type = 'download_job' AND tr.subject_id = ij.download_job_id
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS task_events")
    op.execute("DROP TABLE IF EXISTS task_runs")
