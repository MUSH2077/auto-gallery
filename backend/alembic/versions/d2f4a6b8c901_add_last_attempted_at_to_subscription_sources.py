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
    op.add_column(
        "subscription_sources",
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscription_sources", "last_attempted_at")
