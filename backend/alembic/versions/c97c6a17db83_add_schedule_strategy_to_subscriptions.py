"""add_schedule_strategy_to_subscriptions

Revision ID: c97c6a17db83
Revises: c1d2e3f4a5b6
Create Date: 2026-05-31 14:19:27.655397
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c97c6a17db83'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('subscriptions', sa.Column('schedule_mode', sa.String(length=20), nullable=True,
                  comment="NULL=inherit system default, 'interval', 'fixed_time', 'manual'"))
    op.add_column('subscriptions', sa.Column('scheduled_times', sa.String(length=100), nullable=True,
                  comment="NULL=inherit system default, e.g. '03:00,21:00'"))


def downgrade() -> None:
    op.drop_column('subscriptions', 'scheduled_times')
    op.drop_column('subscriptions', 'schedule_mode')
