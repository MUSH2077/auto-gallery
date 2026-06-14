"""add job manifest auth status and indexes

Revision ID: e4f8a12b77c3
Revises: d2f4a6b8c901
Create Date: 2026-06-04 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "e4f8a12b77c3"
down_revision = "d2f4a6b8c901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE download_jobs ADD COLUMN IF NOT EXISTS manifest JSONB")
    op.execute("ALTER TABLE subscription_sources ADD COLUMN IF NOT EXISTS auth_status VARCHAR(30)")
    op.execute("ALTER TABLE subscription_sources ADD COLUMN IF NOT EXISTS auth_error_reason TEXT")
    op.execute("ALTER TABLE subscription_sources ADD COLUMN IF NOT EXISTS last_auth_checked_at TIMESTAMPTZ")

    op.execute("CREATE INDEX IF NOT EXISTS ix_download_jobs_subscription_source_created ON download_jobs (subscription_source_id, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_download_jobs_status ON download_jobs (status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_download_jobs_subscription_status ON download_jobs (subscription_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_import_jobs_status ON import_jobs (status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_subscription_sources_subscription ON subscription_sources (subscription_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_subscription_sources_source_url ON subscription_sources (source_url)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_works_created_at ON works (created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_work_tags_tag_id ON work_tags (tag_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_work_tags_tag_id")
    op.execute("DROP INDEX IF EXISTS ix_works_created_at")
    op.execute("DROP INDEX IF EXISTS ix_subscription_sources_source_url")
    op.execute("DROP INDEX IF EXISTS ix_subscription_sources_subscription")
    op.execute("DROP INDEX IF EXISTS ix_import_jobs_status")
    op.execute("DROP INDEX IF EXISTS ix_download_jobs_subscription_status")
    op.execute("DROP INDEX IF EXISTS ix_download_jobs_status")
    op.execute("DROP INDEX IF EXISTS ix_download_jobs_subscription_source_created")

    op.execute("ALTER TABLE subscription_sources DROP COLUMN IF EXISTS last_auth_checked_at")
    op.execute("ALTER TABLE subscription_sources DROP COLUMN IF EXISTS auth_error_reason")
    op.execute("ALTER TABLE subscription_sources DROP COLUMN IF EXISTS auth_status")
    op.execute("ALTER TABLE download_jobs DROP COLUMN IF EXISTS manifest")
