"""add curation query indexes

Revision ID: d1e2f3a4b5c6
Revises: c0e1f2a3b4c5
Create Date: 2026-06-13
"""

from typing import Sequence, Union

from alembic import op


revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, None] = "c0e1f2a3b4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_curation_changes_subject_commit",
        "curation_changes",
        ["subject_type", "subject_id", "commit_id"],
    )
    op.create_index(
        "ix_curation_changes_commit_created",
        "curation_changes",
        ["commit_id", "created_at"],
    )
    op.create_index(
        "ix_curation_commits_status_occurred",
        "curation_commits",
        ["status", "occurred_at"],
    )
    op.create_index(
        "ix_curation_commits_trigger_occurred",
        "curation_commits",
        ["trigger", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_curation_commits_trigger_occurred", table_name="curation_commits")
    op.drop_index("ix_curation_commits_status_occurred", table_name="curation_commits")
    op.drop_index("ix_curation_changes_commit_created", table_name="curation_changes")
    op.drop_index("ix_curation_changes_subject_commit", table_name="curation_changes")
