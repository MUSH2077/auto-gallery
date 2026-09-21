"""index Gitllery build and unplanned-intent status paths

Revision ID: ff57a91bcd35
Revises: fe46f80abc24
Create Date: 2026-09-21
"""

from typing import Sequence, Union

from alembic import op


revision: str = "ff57a91bcd35"
down_revision: Union[str, None] = "fe46f80abc24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_gitllery_projection_targets_intent_id ON gitllery_projection_targets (intent_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_gitllery_builds_recent ON gitllery_builds (created_at DESC, id DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_gitllery_builds_recent")
    op.execute("DROP INDEX IF EXISTS ix_gitllery_projection_targets_intent_id")
