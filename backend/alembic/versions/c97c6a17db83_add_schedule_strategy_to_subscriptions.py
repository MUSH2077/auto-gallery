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
    op.execute(
        "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS schedule_mode "
        "VARCHAR(20)"
    )
    op.execute(
        "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS scheduled_times "
        "VARCHAR(100)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE subscriptions DROP COLUMN IF EXISTS scheduled_times")
    op.execute("ALTER TABLE subscriptions DROP COLUMN IF EXISTS schedule_mode")
