"""add_subscription_sync_interval_and_danbooru_artist_id

Revision ID: ea15eaa99d22
Revises: 49d9c60be03c
Create Date: 2026-05-16

Adds subscription.sync_interval_hours column (default 6h).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'ea15eaa99d22'
down_revision: Union[str, None] = '49d9c60be03c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('subscriptions', sa.Column('sync_interval_hours', sa.Integer(), nullable=False, server_default=sa.text('6')))


def downgrade() -> None:
    op.drop_column('subscriptions', 'sync_interval_hours')
