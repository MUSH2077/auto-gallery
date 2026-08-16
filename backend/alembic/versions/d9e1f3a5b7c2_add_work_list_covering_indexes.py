"""add adaptive pipeline projections and work-list indexes

Revision ID: d9e1f3a5b7c2
Revises: c8e4f2a6d901
Create Date: 2026-08-09

The existing library tables are large on production NAS installations.  All
indexes added to those tables are therefore built outside Alembic's transaction
with ``CONCURRENTLY``.  New outbox tables are small and can use ordinary DDL.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d9e1f3a5b7c2"
down_revision: Union[str, None] = "c8e4f2a6d901"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CONCURRENT_INDEXES = (
    """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_work_sources_source_work_id
    ON work_sources (source, work_id)
    """,
    """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_work_sources_identity_work
    ON work_sources (source, source_creator_id, work_id)
    """,
    """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_work_sources_normalized_url_work
    ON work_sources (
      (lower(rtrim(btrim(source_url), '/'))),
      work_id
    )
    WHERE source_url IS NOT NULL
    """,
    """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_asset_sources_work_source_asset
    ON asset_sources (work_source_id, asset_id)
    """,
    """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_source_creators_identity_cover
    ON source_creators (source, source_creator_id)
    INCLUDE (creator_id, display_name)
    """,
    """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_subscription_sources_identity_id
    ON subscription_sources (source, source_creator_id, id)
    """,
    """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_subscription_sources_normalized_url_id
    ON subscription_sources (
      (lower(rtrim(btrim(source_url), '/'))),
      id
    )
    WHERE source_url IS NOT NULL
    """,
    """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_download_jobs_subscription_latest
    ON download_jobs (subscription_id, created_at DESC, id DESC)
    INCLUDE (status)
    """,
    """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_works_created_id
    ON works (created_at, id)
    """,
    """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_works_updated_id
    ON works (updated_at, id)
    """,
    """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_works_posted_id_asc
    ON works (posted_at ASC NULLS LAST, id ASC)
    """,
    """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_works_posted_id_desc
    ON works (posted_at DESC NULLS LAST, id DESC)
    """,
    """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_works_title_id_asc
    ON works (title ASC NULLS LAST, id ASC)
    """,
    """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_works_title_id_desc
    ON works (title DESC NULLS LAST, id DESC)
    """,
    """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_work_curation_states_nonvisible_work
    ON work_curation_states (work_id)
    WHERE visibility <> 'visible'
    """,
    """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_work_curation_states_trashed_work
    ON work_curation_states (work_id)
    WHERE visibility = 'trashed'
    """,
    """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_storage_artifacts_pending_import
    ON storage_artifacts (storage_root, artifact_type, state, id)
    WHERE state IN ('new', 'importing')
    """,
)


_CONCURRENT_INDEX_NAMES = (
    "ix_work_sources_source_work_id",
    "ix_work_sources_identity_work",
    "ix_work_sources_normalized_url_work",
    "ix_asset_sources_work_source_asset",
    "ix_source_creators_identity_cover",
    "ix_subscription_sources_identity_id",
    "ix_subscription_sources_normalized_url_id",
    "ix_download_jobs_subscription_latest",
    "ix_works_created_id",
    "ix_works_updated_id",
    "ix_works_posted_id_asc",
    "ix_works_posted_id_desc",
    "ix_works_title_id_asc",
    "ix_works_title_id_desc",
    "ix_work_curation_states_nonvisible_work",
    "ix_work_curation_states_trashed_work",
    "ix_storage_artifacts_pending_import",
)


def upgrade() -> None:
    # The extension is loaded by the PostgreSQL command line in Compose.  It is
    # intentionally created here so pg_stat_statements sampling is controlled
    # by the application migration rather than by an ad-hoc admin command.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")

    op.execute(
        """
        ALTER TABLE assets
          ADD COLUMN IF NOT EXISTS derivative_version VARCHAR(80),
          ADD COLUMN IF NOT EXISTS derivative_source_size BIGINT,
          ADD COLUMN IF NOT EXISTS derivative_source_mtime_ns BIGINT,
          ADD COLUMN IF NOT EXISTS phash_version VARCHAR(80)
        """
    )
    op.execute(
        """
        ALTER TABLE task_runs
          ADD COLUMN IF NOT EXISTS resource_state VARCHAR(20)
            NOT NULL DEFAULT 'waiting',
          ADD COLUMN IF NOT EXISTS resource_reason VARCHAR(500)
        """
    )
    op.execute(
        """
        UPDATE task_runs
        SET resource_state = CASE
          WHEN status = 'running' THEN 'running'
          WHEN status = 'enqueued' THEN 'waiting'
          ELSE 'yielded'
        END
        WHERE resource_state = 'waiting'
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS media_derivative_outbox (
          id UUID PRIMARY KEY,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          asset_id UUID NOT NULL UNIQUE
            REFERENCES assets(id) ON DELETE CASCADE,
          requested JSONB NOT NULL DEFAULT '{}'::jsonb,
          algorithm_version VARCHAR(80) NOT NULL DEFAULT 'media-v1',
          source_size BIGINT,
          source_mtime_ns BIGINT,
          state VARCHAR(20) NOT NULL DEFAULT 'pending'
            CONSTRAINT ck_media_derivative_outbox_state
            CHECK (state IN ('pending', 'processing', 'complete', 'failed')),
          attempts INTEGER NOT NULL DEFAULT 0,
          available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          lease_expires_at TIMESTAMPTZ,
          completed_at TIMESTAMPTZ,
          last_error TEXT
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_media_derivative_outbox_ready
        ON media_derivative_outbox (available_at, id)
        WHERE state IN ('pending', 'failed')
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_media_derivative_outbox_lease
        ON media_derivative_outbox (lease_expires_at, id)
        WHERE state = 'processing'
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gitllery_projection_outbox (
          id UUID PRIMARY KEY,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          commit_id UUID NOT NULL UNIQUE
            REFERENCES curation_commits(id) ON DELETE CASCADE,
          repository_id UUID,
          state VARCHAR(20) NOT NULL DEFAULT 'pending'
            CONSTRAINT ck_gitllery_projection_outbox_state
            CHECK (state IN ('pending', 'processing', 'complete', 'failed')),
          attempts INTEGER NOT NULL DEFAULT 0,
          available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          lease_expires_at TIMESTAMPTZ,
          completed_at TIMESTAMPTZ,
          last_error TEXT,
          projection_stats JSONB
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_gitllery_projection_outbox_ready
        ON gitllery_projection_outbox (available_at, id)
        WHERE state IN ('pending', 'failed')
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_gitllery_projection_outbox_lease
        ON gitllery_projection_outbox (lease_expires_at, id)
        WHERE state = 'processing'
        """
    )
    # Every curation commit must have a disk-projection intent in the same
    # PostgreSQL transaction, including legacy API paths that still call the
    # best-effort Python wake-up only after commit.  Redis remains only a wake
    # signal; this trigger closes the authoritative DB -> outbox crash gap.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enqueue_gitllery_projection_outbox()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          INSERT INTO gitllery_projection_outbox (
            id, created_at, updated_at, commit_id, state, attempts, available_at
          ) VALUES (
            NEW.id, now(), now(), NEW.id, 'pending', 0, now()
          ) ON CONFLICT (commit_id) DO NOTHING;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_curation_commit_gitllery_outbox ON curation_commits")
    op.execute(
        """
        CREATE TRIGGER trg_curation_commit_gitllery_outbox
        AFTER INSERT ON curation_commits
        FOR EACH ROW EXECUTE FUNCTION enqueue_gitllery_projection_outbox()
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS import_curation_outbox (
          id UUID PRIMARY KEY,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          work_id UUID NOT NULL UNIQUE REFERENCES works(id) ON DELETE CASCADE,
          creator_id UUID,
          repository_id UUID,
          source VARCHAR(50),
          source_work_id VARCHAR(255),
          batch_key VARCHAR(64),
          state VARCHAR(20) NOT NULL DEFAULT 'pending'
            CONSTRAINT ck_import_curation_outbox_state
            CHECK (state IN ('pending', 'processing', 'complete', 'failed')),
          attempts INTEGER NOT NULL DEFAULT 0,
          available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          lease_expires_at TIMESTAMPTZ,
          completed_at TIMESTAMPTZ,
          curation_commit_id UUID
            REFERENCES curation_commits(id) ON DELETE SET NULL,
          last_error TEXT
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_import_curation_outbox_ready
        ON import_curation_outbox (available_at, id)
        WHERE state IN ('pending', 'failed')
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_import_curation_outbox_lease
        ON import_curation_outbox (lease_expires_at, id)
        WHERE state = 'processing'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_import_curation_outbox_batch_key
        ON import_curation_outbox (batch_key)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS search_projection_outbox (
          id UUID PRIMARY KEY,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          index_uid VARCHAR(128) NOT NULL,
          entity_id VARCHAR(64) NOT NULL,
          action VARCHAR(20) NOT NULL DEFAULT 'upsert'
            CONSTRAINT ck_search_projection_outbox_action
            CHECK (action IN ('upsert', 'delete')),
          version BIGINT NOT NULL DEFAULT 1,
          attempts INTEGER NOT NULL DEFAULT 0,
          available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          lease_until TIMESTAMPTZ,
          completed_at TIMESTAMPTZ,
          last_error TEXT,
          CONSTRAINT uq_search_projection_outbox_entity
            UNIQUE (index_uid, entity_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_search_projection_outbox_ready
        ON search_projection_outbox (available_at, updated_at, id)
        WHERE completed_at IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_search_projection_outbox_rebuild_replay
        ON search_projection_outbox (index_uid, updated_at, id)
        """
    )

    # Leaving Alembic's transaction here is deliberate: PostgreSQL rejects
    # CREATE INDEX CONCURRENTLY inside a transaction block.  This also prevents
    # long relation locks while indexing the existing 67k/470k-row tables.
    with op.get_context().autocommit_block():
        for name, statement in zip(
            _CONCURRENT_INDEX_NAMES,
            _CONCURRENT_INDEXES,
            strict=True,
        ):
            # A cancelled CREATE INDEX CONCURRENTLY leaves an invalid catalog
            # entry.  IF NOT EXISTS would silently retain it on retry, so clean
            # only invalid remnants before attempting the resumable build.
            valid = op.get_bind().execute(
                sa.text(
                    """
                    SELECT idx.indisvalid
                    FROM pg_index AS idx
                    JOIN pg_class AS relation ON relation.oid = idx.indexrelid
                    JOIN pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE relation.relname = :name
                      AND namespace.nspname = current_schema()
                    """
                ),
                {"name": name},
            ).scalar_one_or_none()
            if valid is False:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(statement)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in reversed(_CONCURRENT_INDEX_NAMES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")

    op.execute("DROP TRIGGER IF EXISTS trg_curation_commit_gitllery_outbox ON curation_commits")
    op.execute("DROP FUNCTION IF EXISTS enqueue_gitllery_projection_outbox()")
    op.execute("DROP TABLE IF EXISTS search_projection_outbox")
    op.execute("DROP TABLE IF EXISTS import_curation_outbox")
    op.execute("DROP TABLE IF EXISTS gitllery_projection_outbox")
    op.execute("DROP TABLE IF EXISTS media_derivative_outbox")
    op.execute("ALTER TABLE task_runs DROP COLUMN IF EXISTS resource_reason")
    op.execute("ALTER TABLE task_runs DROP COLUMN IF EXISTS resource_state")
    op.execute("ALTER TABLE assets DROP COLUMN IF EXISTS phash_version")
    op.execute("ALTER TABLE assets DROP COLUMN IF EXISTS derivative_source_mtime_ns")
    op.execute("ALTER TABLE assets DROP COLUMN IF EXISTS derivative_source_size")
    op.execute("ALTER TABLE assets DROP COLUMN IF EXISTS derivative_version")
    # pg_stat_statements may be shared by another stack/database user.  Do not
    # remove the extension on downgrade.
