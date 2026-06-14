"""add curation dedupe key

Revision ID: c0e1f2a3b4c5
Revises: b9d4e7a1c2f3
Create Date: 2026-06-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c0e1f2a3b4c5"
down_revision: Union[str, None] = "b9d4e7a1c2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE curation_commits ADD COLUMN IF NOT EXISTS dedupe_key VARCHAR(500)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_curation_commits_dedupe_key ON curation_commits (dedupe_key)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_curation_commits_dedupe_key")
    op.execute("ALTER TABLE curation_commits DROP COLUMN IF EXISTS dedupe_key")
