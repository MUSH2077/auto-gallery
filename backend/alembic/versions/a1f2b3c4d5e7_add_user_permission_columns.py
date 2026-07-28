"""add user permission columns

Revision ID: a1f2b3c4d5e7
Revises: f1a2b3c4d5e6
Create Date: 2026-07-16 00:00:00.000000

"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1f2b3c4d5e7"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "users",
        sa.Column(
            "permissions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "preferences",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "users",
        sa.Column("nsfw_visible", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "users",
        sa.Column("upload_quota_bytes", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("upload_used_bytes", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "users",
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Every pre-existing user predates the multi-user feature and was the
    # (sole) admin — backfill so nobody is locked out of admin-only areas.
    op.execute("UPDATE users SET is_admin = true")


def downgrade() -> None:
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "upload_used_bytes")
    op.drop_column("users", "upload_quota_bytes")
    op.drop_column("users", "nsfw_visible")
    op.drop_column("users", "preferences")
    op.drop_column("users", "permissions")
    op.drop_column("users", "is_admin")
