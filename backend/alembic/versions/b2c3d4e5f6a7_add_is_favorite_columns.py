"""add_is_favorite_columns

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-21

Add is_favorite boolean column to creators and works tables.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE creators ADD COLUMN IF NOT EXISTS is_favorite "
        "BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE works ADD COLUMN IF NOT EXISTS is_favorite "
        "BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE works DROP COLUMN IF EXISTS is_favorite")
    op.execute("ALTER TABLE creators DROP COLUMN IF EXISTS is_favorite")
