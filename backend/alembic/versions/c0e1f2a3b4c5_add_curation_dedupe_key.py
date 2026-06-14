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
    op.add_column("curation_commits", sa.Column("dedupe_key", sa.String(length=500), nullable=True))
    op.create_index("ix_curation_commits_dedupe_key", "curation_commits", ["dedupe_key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_curation_commits_dedupe_key", table_name="curation_commits")
    op.drop_column("curation_commits", "dedupe_key")
