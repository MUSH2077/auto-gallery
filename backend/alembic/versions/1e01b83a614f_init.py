"""init

Revision ID: 1e01b83a614f
Revises:
Create Date: 2025-04-20 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "1e01b83a614f"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(name):
    conn = op.get_bind()
    insp = sa.inspect(conn)
    return name in insp.get_table_names()


def upgrade() -> None:
    if not _table_exists("assets"):
        op.create_table("assets",
            sa.Column("file_path", sa.Text(), nullable=True),
            sa.Column("file_name", sa.Text(), nullable=True),
            sa.Column("file_size", sa.BigInteger(), nullable=True),
            sa.Column("mime_type", sa.String(100), nullable=True),
            sa.Column("width", sa.Integer(), nullable=True),
            sa.Column("height", sa.Integer(), nullable=True),
            sa.Column("duration", sa.Float(), nullable=True),
            sa.Column("sha256", sa.String(64), nullable=True),
            sa.Column("phash", sa.String(64), nullable=True),
            sa.Column("thumb_sm_path", sa.Text(), nullable=True),
            sa.Column("thumb_md_path", sa.Text(), nullable=True),
            sa.Column("thumb_lg_path", sa.Text(), nullable=True),
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _table_exists("creators"):
        op.create_table("creators",
            sa.Column("name", sa.String(500), nullable=True),
            sa.Column("display_name", sa.String(500), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("thumbnail_url", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _table_exists("naming_templates"):
        op.create_table("naming_templates",
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("source", sa.String(50), nullable=False),
            sa.Column("template", sa.Text(), nullable=False),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _table_exists("tags"):
        op.create_table("tags",
            sa.Column("normalized_name", sa.String(200), nullable=False),
            sa.Column("category", sa.String(50), nullable=True),
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("normalized_name"),
        )
    if not _table_exists("works"):
        op.create_table("works",
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("is_nsfw", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("thumbnail_asset_id", sa.Uuid(), nullable=True),
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _table_exists("creator_links"):
        op.create_table("creator_links",
            sa.Column("creator_id", sa.Uuid(), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("link_type", sa.String(50), nullable=False),
            sa.Column("source", sa.String(50), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("is_verified", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["creator_id"], ["creators.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _table_exists("source_creators"):
        op.create_table("source_creators",
            sa.Column("creator_id", sa.Uuid(), nullable=False),
            sa.Column("source", sa.String(50), nullable=False),
            sa.Column("source_creator_id", sa.Text(), nullable=False),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("display_name", sa.String(500), nullable=True),
            sa.Column("raw_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["creator_id"], ["creators.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("source", "source_creator_id", name="uq_source_creators_source_id"),
        )
    if not _table_exists("subscriptions"):
        op.create_table("subscriptions",
            sa.Column("creator_id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.String(500), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("sync_enabled", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["creator_id"], ["creators.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("creator_id", name="uq_subscriptions_creator"),
        )
    if not _table_exists("work_sources"):
        op.create_table("work_sources",
            sa.Column("work_id", sa.Uuid(), nullable=False),
            sa.Column("source", sa.String(50), nullable=False),
            sa.Column("source_work_id", sa.Text(), nullable=False),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("source_creator_id", sa.Text(), nullable=True),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("raw_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["work_id"], ["works.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("source", "source_work_id", name="uq_work_sources_source_id"),
        )
    if not _table_exists("work_tags"):
        op.create_table("work_tags",
            sa.Column("work_id", sa.Uuid(), nullable=False),
            sa.Column("tag_id", sa.Uuid(), nullable=False),
            sa.Column("source", sa.String(50), nullable=True),
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["tag_id"], ["tags.id"]),
            sa.ForeignKeyConstraint(["work_id"], ["works.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("work_id", "tag_id", name="uq_work_tags_work_tag"),
        )
    if not _table_exists("asset_sources"):
        op.create_table("asset_sources",
            sa.Column("asset_id", sa.Uuid(), nullable=False),
            sa.Column("work_source_id", sa.Uuid(), nullable=False),
            sa.Column("source", sa.String(50), nullable=False),
            sa.Column("source_asset_id", sa.Text(), nullable=True),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("raw_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["asset_id"], ["assets.id"]),
            sa.ForeignKeyConstraint(["work_source_id"], ["work_sources.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("source", "source_asset_id", name="uq_asset_sources_source_id"),
        )
    if not _table_exists("subscription_sources"):
        op.create_table("subscription_sources",
            sa.Column("subscription_id", sa.Uuid(), nullable=False),
            sa.Column("source", sa.String(50), nullable=False),
            sa.Column("source_creator_id", sa.Text(), nullable=True),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("last_successful_auth", sa.DateTime(timezone=True), nullable=True),
            sa.Column("auth_healthy", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("subscription_id", "source", name="uq_subscription_sources_sub_source"),
        )
    if not _table_exists("work_source_tags"):
        op.create_table("work_source_tags",
            sa.Column("work_source_id", sa.Uuid(), nullable=False),
            sa.Column("tag_id", sa.Uuid(), nullable=False),
            sa.Column("source", sa.String(50), nullable=True),
            sa.Column("original_name", sa.Text(), nullable=True),
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["tag_id"], ["tags.id"]),
            sa.ForeignKeyConstraint(["work_source_id"], ["work_sources.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("work_source_id", "tag_id", name="uq_work_source_tags_source_tag"),
        )
    if not _table_exists("download_jobs"):
        op.create_table("download_jobs",
            sa.Column("subscription_id", sa.Uuid(), nullable=False),
            sa.Column("subscription_source_id", sa.Uuid(), nullable=False),
            sa.Column("source", sa.String(50), nullable=False),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("status", sa.String(50), nullable=False, server_default="queued"),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error_log", sa.Text(), nullable=True),
            sa.Column("gallerydl_config_path", sa.Text(), nullable=True),
            sa.Column("download_dir", sa.Text(), nullable=True),
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"]),
            sa.ForeignKeyConstraint(["subscription_source_id"], ["subscription_sources.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _table_exists("import_jobs"):
        op.create_table("import_jobs",
            sa.Column("download_job_id", sa.Uuid(), nullable=False),
            sa.Column("status", sa.String(50), nullable=False, server_default="queued"),
            sa.Column("error_log", sa.Text(), nullable=True),
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["download_job_id"], ["download_jobs.id"]),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS import_jobs")
    op.execute("DROP TABLE IF EXISTS download_jobs")
    op.execute("DROP TABLE IF EXISTS work_source_tags")
    op.execute("DROP TABLE IF EXISTS subscription_sources")
    op.execute("DROP TABLE IF EXISTS asset_sources")
    op.execute("DROP TABLE IF EXISTS work_tags")
    op.execute("DROP TABLE IF EXISTS work_sources")
    op.execute("DROP TABLE IF EXISTS subscriptions")
    op.execute("DROP TABLE IF EXISTS source_creators")
    op.execute("DROP TABLE IF EXISTS creator_links")
    op.execute("DROP TABLE IF EXISTS works")
    op.execute("DROP TABLE IF EXISTS tags")
    op.execute("DROP TABLE IF EXISTS naming_templates")
    op.execute("DROP TABLE IF EXISTS creators")
    op.execute("DROP TABLE IF EXISTS assets")
