"""add_danbooru_artist_id_to_creator

Revision ID: 7213b2b34d8a
Revises: ea15eaa99d22
Create Date: 2026-05-16

Adds creators.danbooru_artist_id column for Danbooru identity mapping.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7213b2b34d8a'
down_revision: Union[str, None] = 'ea15eaa99d22'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('creators', sa.Column('danbooru_artist_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('creators', 'danbooru_artist_id')
