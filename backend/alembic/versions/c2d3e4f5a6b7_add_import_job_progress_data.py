"""add import job progress data

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-06-18 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("import_jobs", "progress_data"):
        op.add_column("import_jobs", sa.Column("progress_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    if _has_column("import_jobs", "progress_data"):
        op.drop_column("import_jobs", "progress_data")
