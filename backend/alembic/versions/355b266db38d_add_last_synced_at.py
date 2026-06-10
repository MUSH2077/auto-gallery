"""add last_synced_at to subscription_sources

Revision ID: 355b266db38d
Revises: a7c9d2e4f601
Create Date: 2026-06-10 13:43:07
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '355b266db38d'
down_revision: Union[str, None] = 'a7c9d2e4f601'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('subscription_sources',
        sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('subscription_sources', 'last_synced_at')
