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
    op.add_column('creators', sa.Column('is_favorite', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.add_column('works', sa.Column('is_favorite', sa.Boolean(), nullable=False, server_default=sa.text('false')))


def downgrade() -> None:
    op.drop_column('works', 'is_favorite')
    op.drop_column('creators', 'is_favorite')
