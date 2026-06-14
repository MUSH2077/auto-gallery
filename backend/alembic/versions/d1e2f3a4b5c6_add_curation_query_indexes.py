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
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_curation_changes_subject_commit "
        "ON curation_changes (subject_type, subject_id, commit_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_curation_changes_commit_created "
        "ON curation_changes (commit_id, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_curation_commits_status_occurred "
        "ON curation_commits (status, occurred_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_curation_commits_trigger_occurred "
        "ON curation_commits (trigger, occurred_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_curation_commits_trigger_occurred")
    op.execute("DROP INDEX IF EXISTS ix_curation_commits_status_occurred")
    op.execute("DROP INDEX IF EXISTS ix_curation_changes_commit_created")
    op.execute("DROP INDEX IF EXISTS ix_curation_changes_subject_commit")
