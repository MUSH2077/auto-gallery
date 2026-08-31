"""allow manual download jobs without a subscription source

Revision ID: b8d0f2a4c6e9
Revises: a6c8e0f2b4d7
"""

from alembic import op
import sqlalchemy as sa


revision = "b8d0f2a4c6e9"
down_revision = "a6c8e0f2b4d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "download_jobs",
        "subscription_source_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )


def downgrade() -> None:
    # Manual uploads intentionally have no source-row FK. Refuse to erase that
    # valid state or let PostgreSQL fail later with a context-free NOT NULL
    # violation; operators must explicitly remove those rows before downgrade.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM download_jobs
            WHERE subscription_source_id IS NULL
          ) THEN
            RAISE EXCEPTION
              'NULL download job source references prevent downgrade; '
              'archive or explicitly remove those download_jobs rows first';
          END IF;
        END
        $$
        """
    )
    op.alter_column(
        "download_jobs",
        "subscription_source_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
