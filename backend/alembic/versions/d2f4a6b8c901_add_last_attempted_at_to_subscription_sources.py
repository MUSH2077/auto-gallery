"""add last_attempted_at to subscription_sources

Revision ID: d2f4a6b8c901
Revises: c97c6a17db83
Create Date: 2026-06-04 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "d2f4a6b8c901"
down_revision = "c97c6a17db83"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE subscription_sources ADD COLUMN IF NOT EXISTS "
        "last_attempted_at TIMESTAMPTZ"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE subscription_sources DROP COLUMN IF EXISTS last_attempted_at"
    )
