"""add Gitllery v1 segment projection state

Revision ID: f0d2e4a6b8c1
Revises: e9c1d3f5a7b0
Create Date: 2026-08-11
"""

from typing import Sequence, Union

from alembic import op


revision: str = "f0d2e4a6b8c1"
down_revision: Union[str, None] = "e9c1d3f5a7b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE curation_changes ADD COLUMN IF NOT EXISTS sequence INTEGER"
    )
    op.execute(
        """
        CREATE TABLE gitllery_projection_targets (
          id UUID PRIMARY KEY,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          intent_id UUID NOT NULL REFERENCES gitllery_projection_outbox(id) ON DELETE CASCADE,
          commit_id UUID NOT NULL REFERENCES curation_commits(id) ON DELETE CASCADE,
          commit_created_at TIMESTAMPTZ NOT NULL,
          repository_key VARCHAR(500) NOT NULL,
          source VARCHAR(50) NOT NULL,
          creator_dir VARCHAR(1000) NOT NULL,
          payload JSONB NOT NULL,
          state VARCHAR(20) NOT NULL DEFAULT 'pending'
            CONSTRAINT ck_gitllery_projection_target_state
            CHECK (state IN ('pending','processing','complete','failed','blocked')),
          attempts INTEGER NOT NULL DEFAULT 0,
          available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          lease_token UUID,
          lease_expires_at TIMESTAMPTZ,
          completed_at TIMESTAMPTZ,
          segment_digest VARCHAR(64),
          last_error TEXT,
          CONSTRAINT uq_gitllery_projection_target_commit_repo
            UNIQUE (commit_id, repository_key)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_gitllery_projection_targets_ready
        ON gitllery_projection_targets (available_at, id)
        WHERE state IN ('pending','failed')
        """
    )
    op.execute(
        """
        CREATE INDEX ix_gitllery_projection_targets_repo_order
        ON gitllery_projection_targets (repository_key, commit_created_at, commit_id)
        """
    )
    op.execute(
        """
        CREATE TABLE gitllery_repository_state (
          id UUID PRIMARY KEY,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          repository_key VARCHAR(500) NOT NULL UNIQUE,
          source VARCHAR(50) NOT NULL,
          creator_dir VARCHAR(1000) NOT NULL,
          product_version VARCHAR(20) NOT NULL DEFAULT 'v1',
          format_id VARCHAR(80) NOT NULL DEFAULT 'gitllery-segment',
          format_revision INTEGER NOT NULL DEFAULT 1,
          mode VARCHAR(20) NOT NULL DEFAULT 'capture'
            CONSTRAINT ck_gitllery_repository_state_mode
            CHECK (mode IN ('capture','shadow','active','blocked')),
          generation VARCHAR(100) NOT NULL,
          head_segment VARCHAR(64),
          last_complete_commit_id UUID REFERENCES curation_commits(id) ON DELETE SET NULL,
          last_complete_created_at TIMESTAMPTZ,
          segment_count INTEGER NOT NULL DEFAULT 0,
          commit_count INTEGER NOT NULL DEFAULT 0,
          change_count INTEGER NOT NULL DEFAULT 0,
          last_verified_at TIMESTAMPTZ,
          last_error TEXT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE gitllery_builds (
          id UUID PRIMARY KEY,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          kind VARCHAR(20) NOT NULL
            CONSTRAINT ck_gitllery_build_kind CHECK (kind IN ('build','verify','restore')),
          state VARCHAR(20) NOT NULL DEFAULT 'pending'
            CONSTRAINT ck_gitllery_build_state
            CHECK (state IN ('pending','running','staged','complete','failed','cancelled')),
          generation VARCHAR(100) NOT NULL,
          repository_key VARCHAR(500),
          high_water_commit_id UUID REFERENCES curation_commits(id) ON DELETE SET NULL,
          cursor_created_at TIMESTAMPTZ,
          cursor_commit_id UUID,
          summary_hash VARCHAR(64),
          stats JSONB,
          last_error TEXT
        )
        """
    )
    # Capture the authoritative historical prefix.  This is a single bounded
    # PostgreSQL INSERT; the governed worker resolves repositories and writes
    # segments asynchronously in 20-second slices after the migration.
    op.execute(
        """
        INSERT INTO gitllery_projection_outbox (
          id, created_at, updated_at, commit_id, state, attempts, available_at
        )
        SELECT id, now(), now(), id, 'pending', 0, now()
        FROM curation_commits
        ON CONFLICT (commit_id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE gitllery_projection_outbox
        SET state = 'pending',
            attempts = 0,
            available_at = now(),
            lease_expires_at = NULL,
            completed_at = NULL,
            last_error = NULL,
            projection_stats = NULL,
            updated_at = now()
        WHERE projection_stats IS NULL
           OR projection_stats->>'format' IS DISTINCT FROM 'gitllery-segment'
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS gitllery_builds")
    op.execute("DROP TABLE IF EXISTS gitllery_repository_state")
    op.execute("DROP TABLE IF EXISTS gitllery_projection_targets")
    op.execute("ALTER TABLE curation_changes DROP COLUMN IF EXISTS sequence")
