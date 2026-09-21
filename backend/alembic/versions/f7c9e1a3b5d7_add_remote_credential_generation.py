"""add durable remote credential generation

Revision ID: f7c9e1a3b5d7
Revises: f4a6c8e0b2d4
Create Date: 2026-08-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f7c9e1a3b5d7"
down_revision: Union[str, None] = "f4a6c8e0b2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "remote_accounts",
        sa.Column(
            "credential_generation",
            sa.Integer(),
            nullable=True,
            server_default=sa.text("0"),
        ),
    )
    op.execute(
        "UPDATE remote_accounts SET credential_generation = 1 "
        "WHERE credential_ciphertext IS NOT NULL"
    )
    op.alter_column(
        "remote_accounts",
        "credential_generation",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=sa.text("0"),
    )
    op.create_check_constraint(
        "ck_remote_accounts_credential_generation",
        "remote_accounts",
        "credential_generation >= 0",
    )

    op.add_column(
        "download_jobs",
        sa.Column("triggering_credential_generation", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_download_jobs_triggering_credential_generation",
        "download_jobs",
        "triggering_credential_generation IS NULL "
        "OR triggering_credential_generation >= 1",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_download_jobs_triggering_credential_generation",
        "download_jobs",
        type_="check",
    )
    op.drop_column("download_jobs", "triggering_credential_generation")
    op.drop_constraint(
        "ck_remote_accounts_credential_generation",
        "remote_accounts",
        type_="check",
    )
    op.drop_column("remote_accounts", "credential_generation")
