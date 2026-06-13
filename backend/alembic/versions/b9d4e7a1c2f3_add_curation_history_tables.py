"""add curation history tables

Revision ID: b9d4e7a1c2f3
Revises: a8b9c0d1e2f3
Create Date: 2026-06-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b9d4e7a1c2f3"
down_revision: Union[str, None] = "a8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "curation_commits",
        sa.Column("parent_commit_id", sa.Uuid(), nullable=True),
        sa.Column("actor_type", sa.String(length=50), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("trigger", sa.String(length=100), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("reverts_commit_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["parent_commit_id"], ["curation_commits.id"]),
        sa.ForeignKeyConstraint(["reverts_commit_id"], ["curation_commits.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_curation_commits_occurred_at", "curation_commits", ["occurred_at"])
    op.create_index("ix_curation_commits_trigger", "curation_commits", ["trigger"])
    op.create_index("ix_curation_commits_status", "curation_commits", ["status"])

    op.create_table(
        "curation_changes",
        sa.Column("commit_id", sa.Uuid(), nullable=False),
        sa.Column("subject_type", sa.String(length=50), nullable=False),
        sa.Column("subject_id", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("before_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("diff", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("impact", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["commit_id"], ["curation_commits.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_curation_changes_commit_id", "curation_changes", ["commit_id"])
    op.create_index("ix_curation_changes_subject", "curation_changes", ["subject_type", "subject_id"])
    op.create_index("ix_curation_changes_action", "curation_changes", ["action"])

    op.create_table(
        "work_curation_states",
        sa.Column("work_id", sa.Uuid(), nullable=False),
        sa.Column("visibility", sa.String(length=50), nullable=False),
        sa.Column("trashed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trashed_by_commit_id", sa.Uuid(), nullable=True),
        sa.Column("restored_by_commit_id", sa.Uuid(), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purged_by_commit_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["purged_by_commit_id"], ["curation_commits.id"]),
        sa.ForeignKeyConstraint(["restored_by_commit_id"], ["curation_commits.id"]),
        sa.ForeignKeyConstraint(["trashed_by_commit_id"], ["curation_commits.id"]),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_id"),
    )
    op.create_index("ix_work_curation_states_visibility", "work_curation_states", ["visibility"])

    op.create_table(
        "creator_curation_states",
        sa.Column("creator_id", sa.Uuid(), nullable=False),
        sa.Column("visibility", sa.String(length=50), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by_commit_id", sa.Uuid(), nullable=True),
        sa.Column("restored_by_commit_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["archived_by_commit_id"], ["curation_commits.id"]),
        sa.ForeignKeyConstraint(["creator_id"], ["creators.id"]),
        sa.ForeignKeyConstraint(["restored_by_commit_id"], ["curation_commits.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("creator_id"),
    )
    op.create_index("ix_creator_curation_states_visibility", "creator_curation_states", ["visibility"])

    op.create_table(
        "asset_storage_states",
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("storage_state", sa.String(length=50), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purged_by_commit_id", sa.Uuid(), nullable=True),
        sa.Column("bytes_reclaimed", sa.Integer(), nullable=False),
        sa.Column("missing_files", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"]),
        sa.ForeignKeyConstraint(["purged_by_commit_id"], ["curation_commits.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id"),
    )
    op.create_index("ix_asset_storage_states_storage_state", "asset_storage_states", ["storage_state"])


def downgrade() -> None:
    op.drop_index("ix_asset_storage_states_storage_state", table_name="asset_storage_states")
    op.drop_table("asset_storage_states")
    op.drop_index("ix_creator_curation_states_visibility", table_name="creator_curation_states")
    op.drop_table("creator_curation_states")
    op.drop_index("ix_work_curation_states_visibility", table_name="work_curation_states")
    op.drop_table("work_curation_states")
    op.drop_index("ix_curation_changes_action", table_name="curation_changes")
    op.drop_index("ix_curation_changes_subject", table_name="curation_changes")
    op.drop_index("ix_curation_changes_commit_id", table_name="curation_changes")
    op.drop_table("curation_changes")
    op.drop_index("ix_curation_commits_status", table_name="curation_commits")
    op.drop_index("ix_curation_commits_trigger", table_name="curation_commits")
    op.drop_index("ix_curation_commits_occurred_at", table_name="curation_commits")
    op.drop_table("curation_commits")
