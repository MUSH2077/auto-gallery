"""enforce private discovery ownership and subscription consistency

Revision ID: f5b7d9e1a3c5
Revises: f4a6c8e0b2d4
Create Date: 2026-08-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f5b7d9e1a3c5"
down_revision: Union[str, None] = "f4a6c8e0b2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Composite foreign keys need unique referenced keys. They make the owner
    # and canonical subscription identity part of every private binding rather
    # than trusting a later service/worker to re-check it.
    op.create_unique_constraint(
        "uq_user_subscriptions_id_owner_subscription",
        "user_subscriptions",
        ["id", "user_id", "subscription_id"],
    )
    op.create_unique_constraint("uq_remote_accounts_id_owner", "remote_accounts", ["id", "user_id"])
    op.create_unique_constraint(
        "uq_subscription_sources_id_subscription",
        "subscription_sources",
        ["id", "subscription_id"],
    )

    op.add_column("user_subscription_sources", sa.Column("user_id", sa.Integer(), nullable=True))
    op.add_column("user_subscription_sources", sa.Column("subscription_id", sa.Uuid(), nullable=True))
    op.execute(
        """
        UPDATE user_subscription_sources AS binding
        SET user_id = membership.user_id,
            subscription_id = membership.subscription_id
        FROM user_subscriptions AS membership
        WHERE membership.id = binding.user_subscription_id
        """
    )
    op.alter_column("user_subscription_sources", "user_id", nullable=False)
    op.alter_column("user_subscription_sources", "subscription_id", nullable=False)
    op.create_foreign_key(
        "fk_user_subscription_sources_user",
        "user_subscription_sources",
        "users",
        ["user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_user_subscription_sources_subscription",
        "user_subscription_sources",
        "subscriptions",
        ["subscription_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_user_subscription_sources_membership_owner",
        "user_subscription_sources",
        "user_subscriptions",
        ["user_subscription_id", "user_id", "subscription_id"],
        ["id", "user_id", "subscription_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_user_subscription_sources_source_subscription",
        "user_subscription_sources",
        "subscription_sources",
        ["subscription_source_id", "subscription_id"],
        ["id", "subscription_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_user_subscription_sources_remote_account_owner",
        "user_subscription_sources",
        "remote_accounts",
        ["remote_account_id", "user_id"],
        ["id", "user_id"],
        ondelete="RESTRICT",
    )

    op.add_column("discovery_candidates", sa.Column("user_id", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE discovery_candidates AS candidate
        SET user_id = account.user_id
        FROM remote_accounts AS account
        WHERE account.id = candidate.remote_account_id
        """
    )
    op.alter_column("discovery_candidates", "user_id", nullable=False)
    op.create_foreign_key(
        "fk_discovery_candidates_user",
        "discovery_candidates",
        "users",
        ["user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_discovery_candidates_account_owner",
        "discovery_candidates",
        "remote_accounts",
        ["remote_account_id", "user_id"],
        ["id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_discovery_candidates_membership_owner",
        "discovery_candidates",
        "user_subscriptions",
        ["user_subscription_id", "user_id", "subscription_id"],
        ["id", "user_id", "subscription_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_discovery_candidates_membership_subscription",
        "discovery_candidates",
        "user_subscription_id IS NULL OR subscription_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_discovery_candidates_membership_subscription", "discovery_candidates", type_="check")
    op.drop_constraint("fk_discovery_candidates_membership_owner", "discovery_candidates", type_="foreignkey")
    op.drop_constraint("fk_discovery_candidates_account_owner", "discovery_candidates", type_="foreignkey")
    op.drop_constraint("fk_discovery_candidates_user", "discovery_candidates", type_="foreignkey")
    op.drop_column("discovery_candidates", "user_id")
    op.drop_constraint("fk_user_subscription_sources_remote_account_owner", "user_subscription_sources", type_="foreignkey")
    op.drop_constraint("fk_user_subscription_sources_source_subscription", "user_subscription_sources", type_="foreignkey")
    op.drop_constraint("fk_user_subscription_sources_membership_owner", "user_subscription_sources", type_="foreignkey")
    op.drop_constraint("fk_user_subscription_sources_subscription", "user_subscription_sources", type_="foreignkey")
    op.drop_constraint("fk_user_subscription_sources_user", "user_subscription_sources", type_="foreignkey")
    op.drop_column("user_subscription_sources", "subscription_id")
    op.drop_column("user_subscription_sources", "user_id")
    op.drop_constraint("uq_subscription_sources_id_subscription", "subscription_sources", type_="unique")
    op.drop_constraint("uq_remote_accounts_id_owner", "remote_accounts", type_="unique")
    op.drop_constraint("uq_user_subscriptions_id_owner_subscription", "user_subscriptions", type_="unique")
