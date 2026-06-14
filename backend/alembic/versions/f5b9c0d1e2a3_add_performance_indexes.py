"""add performance indexes for high-traffic queries

Revision ID: f5b9c0d1e2a3
Revises: e4f8a12b77c3
Create Date: 2026-06-06 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "f5b9c0d1e2a3"
down_revision = "e4f8a12b77c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enable pg_trgm extension for trigram fuzzy search indexes
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ── creators ──────────────────────────────────────────────────────────
    op.execute("CREATE INDEX IF NOT EXISTS ix_creators_name ON creators (name)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_creators_display_name ON creators (display_name)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_creators_danbooru_artist_id ON creators (danbooru_artist_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_creators_is_active ON creators (is_active)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_creators_is_favorite ON creators (is_favorite)")

    # pg_trgm indexes for ILIKE '%search%' acceleration
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_creators_name_trgm "
        "ON creators USING gin (name gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_creators_display_name_trgm "
        "ON creators USING gin (display_name gin_trgm_ops)"
    )

    # ── works ─────────────────────────────────────────────────────────────
    op.execute("CREATE INDEX IF NOT EXISTS ix_works_posted_at ON works (posted_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_works_is_nsfw ON works (is_nsfw)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_works_is_ai_generated ON works (is_ai_generated)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_works_is_favorite ON works (is_favorite)")

    # ── work_sources ──────────────────────────────────────────────────────
    op.execute("CREATE INDEX IF NOT EXISTS ix_work_sources_source ON work_sources (source)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_work_sources_work_id ON work_sources (work_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_work_sources_source_creator_id ON work_sources (source_creator_id)")

    # ── work_tags ─────────────────────────────────────────────────────────
    op.execute("CREATE INDEX IF NOT EXISTS ix_work_tags_work_id ON work_tags (work_id)")

    # ── work_source_tags ──────────────────────────────────────────────────
    op.execute("CREATE INDEX IF NOT EXISTS ix_work_source_tags_work_source_id ON work_source_tags (work_source_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_work_source_tags_tag_id ON work_source_tags (tag_id)")

    # ── asset_sources ─────────────────────────────────────────────────────
    op.execute("CREATE INDEX IF NOT EXISTS ix_asset_sources_work_source_id ON asset_sources (work_source_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_asset_sources_asset_id ON asset_sources (asset_id)")

    # ── subscription_sources ──────────────────────────────────────────────
    op.execute("CREATE INDEX IF NOT EXISTS ix_subscription_sources_source ON subscription_sources (source)")

    # ── download_jobs ─────────────────────────────────────────────────────
    op.execute("CREATE INDEX IF NOT EXISTS ix_download_jobs_subscription_source_id ON download_jobs (subscription_source_id)")

    # ── import_jobs ───────────────────────────────────────────────────────
    op.execute("CREATE INDEX IF NOT EXISTS ix_import_jobs_download_job_id ON import_jobs (download_job_id)")

    # ── source_creators ───────────────────────────────────────────────────
    op.execute("CREATE INDEX IF NOT EXISTS ix_source_creators_creator_id ON source_creators (creator_id)")

    # ── creator_links ─────────────────────────────────────────────────────
    op.execute("CREATE INDEX IF NOT EXISTS ix_creator_links_creator_id ON creator_links (creator_id)")


def downgrade() -> None:
    op.drop_index("ix_creator_links_creator_id", table_name="creator_links")
    op.drop_index("ix_source_creators_creator_id", table_name="source_creators")
    op.drop_index("ix_import_jobs_download_job_id", table_name="import_jobs")
    op.drop_index("ix_download_jobs_subscription_source_id", table_name="download_jobs")
    op.drop_index("ix_subscription_sources_source", table_name="subscription_sources")
    op.drop_index("ix_asset_sources_asset_id", table_name="asset_sources")
    op.drop_index("ix_asset_sources_work_source_id", table_name="asset_sources")
    op.drop_index("ix_work_source_tags_tag_id", table_name="work_source_tags")
    op.drop_index("ix_work_source_tags_work_source_id", table_name="work_source_tags")
    op.drop_index("ix_work_tags_work_id", table_name="work_tags")
    op.drop_index("ix_work_sources_source_creator_id", table_name="work_sources")
    op.drop_index("ix_work_sources_work_id", table_name="work_sources")
    op.drop_index("ix_work_sources_source", table_name="work_sources")
    op.drop_index("ix_works_is_favorite", table_name="works")
    op.drop_index("ix_works_is_ai_generated", table_name="works")
    op.drop_index("ix_works_is_nsfw", table_name="works")
    op.drop_index("ix_works_posted_at", table_name="works")
    op.execute("DROP INDEX IF EXISTS ix_creators_display_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_creators_name_trgm")
    op.drop_index("ix_creators_is_favorite", table_name="creators")
    op.drop_index("ix_creators_is_active", table_name="creators")
    op.drop_index("ix_creators_danbooru_artist_id", table_name="creators")
    op.drop_index("ix_creators_display_name", table_name="creators")
    op.drop_index("ix_creators_name", table_name="creators")
