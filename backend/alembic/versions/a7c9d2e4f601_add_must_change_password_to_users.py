"""add must_change_password to users

Revision ID: a7c9d2e4f601
Revises: f5b9c0d1e2a3
Create Date: 2026-06-07 00:00:00.000000

"""
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c9d2e4f601"
down_revision: Union[str, None] = "f5b9c0d1e2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password "
        "BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS must_change_password")
