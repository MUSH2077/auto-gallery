"""Bind bearer credentials to the current account security version."""

from alembic import op
import sqlalchemy as sa

revision = "0159bc20de46"
down_revision = "0b79d2e4f6a8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("credential_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )


def downgrade():
    op.drop_column("users", "credential_version")
