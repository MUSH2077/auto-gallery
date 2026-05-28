"""add is_ai_generated to works

Revision ID: afa4ea273c76
Revises: b2c3d4e5f6a7
Create Date: 2026-05-24 07:19:50.646189
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'afa4ea273c76'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('works', sa.Column('is_ai_generated', sa.Boolean(), nullable=False, server_default=sa.text('false')))


def downgrade() -> None:
    op.drop_column('works', 'is_ai_generated')
