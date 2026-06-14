"""add_system_settings_table

Revision ID: a1b2c3d4e5f6
Revises: 54fe9ca906e2
Create Date: 2026-05-16 22:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '54fe9ca906e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if "system_settings" not in insp.get_table_names():
        op.create_table(
            "system_settings",
            sa.Column("key", sa.String(100), primary_key=True),
            sa.Column("value", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS system_settings")
