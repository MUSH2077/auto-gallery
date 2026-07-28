"""add durable storage artifact ledger

Revision ID: a6c8e0f2b4d6
Revises: c2d3e4f5a6b7
"""

from alembic import op


revision = "a6c8e0f2b4d6"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS storage_artifacts (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            storage_root VARCHAR(20) NOT NULL,
            file_path VARCHAR(2000) NOT NULL,
            source VARCHAR(50) NOT NULL,
            creator_dir VARCHAR(500) NOT NULL,
            source_work_id VARCHAR(255) NOT NULL,
            file_name VARCHAR(500) NOT NULL,
            artifact_type VARCHAR(30) NOT NULL,
            file_size BIGINT,
            mtime_ns BIGINT,
            content_version VARCHAR(64),
            download_job_id UUID REFERENCES download_jobs(id) ON DELETE SET NULL,
            import_job_id UUID REFERENCES import_jobs(id) ON DELETE SET NULL,
            state VARCHAR(20) NOT NULL DEFAULT 'new',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            lease_expires_at TIMESTAMPTZ,
            CONSTRAINT uq_storage_artifacts_root_path UNIQUE (storage_root, file_path)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_storage_artifacts_download_state ON storage_artifacts (download_job_id, artifact_type, state)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_storage_artifacts_source_work ON storage_artifacts (source, source_work_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_storage_artifacts_lease ON storage_artifacts (state, lease_expires_at)")
    # Older releases could enqueue duplicates during a scheduler race. Preserve
    # the newest active job and make the older rows explicitly recoverable.
    op.execute("""
        WITH ranked AS (
            SELECT id, row_number() OVER (
                PARTITION BY subscription_source_id ORDER BY created_at DESC, id DESC
            ) AS position
            FROM download_jobs
            WHERE subscription_source_id IS NOT NULL
              AND status IN ('enqueued', 'downloading', 'downloaded', 'importing')
        )
        UPDATE download_jobs AS jobs
        SET status = 'stale', error_log = COALESCE(error_log, 'superseded during active-job constraint migration')
        FROM ranked
        WHERE jobs.id = ranked.id AND ranked.position > 1
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_download_jobs_active_source
        ON download_jobs (subscription_source_id)
        WHERE subscription_source_id IS NOT NULL
          AND status IN ('enqueued', 'downloading', 'downloaded', 'importing')
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_download_jobs_active_source")
    op.execute("DROP TABLE IF EXISTS storage_artifacts")
