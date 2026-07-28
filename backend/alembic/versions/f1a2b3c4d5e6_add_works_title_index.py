"""add works.title sort index

The works list can sort by title, but works had no index on title — so
ORDER BY title fell back to a Seq Scan + Sort (O(N log N)) on every
cache-miss, which degrades badly on a large library. created_at and
posted_at (the other sort options) are already indexed.

Revision ID: f1a2b3c4d5e6
Revises: e5f6a7b8c9d0
Create Date: 2026-07-07 00:00:00.000000
"""

from alembic import op


revision = "f1a2b3c4d5e6"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_works_title ON works (title)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_works_title")
