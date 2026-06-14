"""make_source_creator_creator_id_nullable

Revision ID: 49d9c60be03c
Revises: 1e01b83a614f
Create Date: 2026-05-15 07:00:07.003849
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '49d9c60be03c'
down_revision: Union[str, None] = '1e01b83a614f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE source_creators ALTER COLUMN creator_id DROP NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE source_creators SET creator_id = "
        "'00000000-0000-0000-0000-000000000000'::uuid "
        "WHERE creator_id IS NULL"
    )
    op.execute(
        "ALTER TABLE source_creators ALTER COLUMN creator_id SET NOT NULL"
    )
