"""Migrate old status "pending" to "enqueued" to download_jobs and import_jobs

Revision ID: a0b1c2d3e4f5
Revises: a0b1c2d3e4f5
Create Date: 2026-06-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, None] = "a0b1c2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Migrate old "pending" status to "enqueued"
    op.execute(sa.text(
        "UPDATE download_jobs SET status = 'enqueued' WHERE status = 'pending'"
    ))
    op.execute(sa.text(
        "UPDATE import_jobs SET status = 'enqueued' WHERE status = 'pending'"
    ))

def downgrade() -> None:
    # Revert enqueued back to pending
    op.execute(sa.text(
        "UPDATE download_jobs SET status = 'pending' WHERE status = 'enqueued'"
    ))
    op.execute(sa.text(
        "UPDATE import_jobs SET status = 'pending' WHERE status = 'enqueued'"
    ))