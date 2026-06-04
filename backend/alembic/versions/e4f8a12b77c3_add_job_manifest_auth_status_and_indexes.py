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
    op.add_column("download_jobs", sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("subscription_sources", sa.Column("auth_status", sa.String(length=30), nullable=True))
    op.add_column("subscription_sources", sa.Column("auth_error_reason", sa.Text(), nullable=True))
    op.add_column("subscription_sources", sa.Column("last_auth_checked_at", sa.DateTime(timezone=True), nullable=True))

    op.create_index("ix_download_jobs_subscription_source_created", "download_jobs", ["subscription_source_id", "created_at"])
    op.create_index("ix_download_jobs_status", "download_jobs", ["status"])
    op.create_index("ix_download_jobs_subscription_status", "download_jobs", ["subscription_id", "status"])
    op.create_index("ix_import_jobs_status", "import_jobs", ["status"])
    op.create_index("ix_subscription_sources_subscription", "subscription_sources", ["subscription_id"])
    op.create_index("ix_subscription_sources_source_url", "subscription_sources", ["source_url"])
    op.create_index("ix_works_created_at", "works", ["created_at"])
    op.create_index("ix_work_tags_tag_id", "work_tags", ["tag_id"])


def downgrade() -> None:
    op.drop_index("ix_work_tags_tag_id", table_name="work_tags")
    op.drop_index("ix_works_created_at", table_name="works")
    op.drop_index("ix_subscription_sources_source_url", table_name="subscription_sources")
    op.drop_index("ix_subscription_sources_subscription", table_name="subscription_sources")
    op.drop_index("ix_import_jobs_status", table_name="import_jobs")
    op.drop_index("ix_download_jobs_subscription_status", table_name="download_jobs")
    op.drop_index("ix_download_jobs_status", table_name="download_jobs")
    op.drop_index("ix_download_jobs_subscription_source_created", table_name="download_jobs")

    op.drop_column("subscription_sources", "last_auth_checked_at")
    op.drop_column("subscription_sources", "auth_error_reason")
    op.drop_column("subscription_sources", "auth_status")
    op.drop_column("download_jobs", "manifest")
