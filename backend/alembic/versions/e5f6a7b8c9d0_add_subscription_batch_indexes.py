"""add subscription batch sync indexes

Revision ID: e5f6a7b8c9d0
Revises: d3e4f5a6b7c8
Create Date: 2026-06-29 00:00:00.000000
"""

from alembic import op


revision = "e5f6a7b8c9d0"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_subscription_sources_sub_enabled "
        "ON subscription_sources (subscription_id, is_enabled)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_subscription_sources_last_synced "
        "ON subscription_sources (last_synced_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_download_jobs_source_status_created "
        "ON download_jobs (subscription_source_id, status, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_download_jobs_source_status_created")
    op.execute("DROP INDEX IF EXISTS ix_subscription_sources_last_synced")
    op.execute("DROP INDEX IF EXISTS ix_subscription_sources_sub_enabled")
