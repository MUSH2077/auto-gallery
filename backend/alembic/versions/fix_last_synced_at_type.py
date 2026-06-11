"""fix last_synced_at column type in subscriptions

Revision ID: a8b9c0d1e2f3
Revises: 355b266db38d
Create Date: 2026-06-10
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'a8b9c0d1e2f3'
down_revision: Union[str, None] = '355b266db38d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: skip if already DateTime (container rebuild with existing DB)
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name='subscriptions' AND column_name='last_synced_at'"
    ))
    current_type = result.scalar()
    if current_type and 'timestamp' in current_type:
        return
    op.execute(
        "ALTER TABLE subscriptions ALTER COLUMN last_synced_at "
        "TYPE timestamp with time zone USING last_synced_at::timestamp with time zone"
    )


def downgrade() -> None:
    op.alter_column('subscriptions', 'last_synced_at',
        type_=sa.String(), existing_type=sa.DateTime(timezone=True))
