"""enforce subscription schedule consistency

Revision ID: f4c6d8e0a2b3
Revises: f3b5d7e9a1c2
"""

from alembic import op


revision = "f4c6d8e0a2b3"
down_revision = "f3b5d7e9a1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # "inherit" was accepted by some clients even though NULL is canonical.
    # Repair the single known half-updated manual row before adding the check.
    op.execute("UPDATE subscriptions SET schedule_mode = NULL WHERE schedule_mode = 'inherit'")
    op.execute(
        "UPDATE subscriptions SET schedule_mode = NULL "
        "WHERE schedule_mode = 'manual' AND sync_enabled IS TRUE"
    )
    op.create_check_constraint(
        "ck_subscriptions_schedule_mode",
        "subscriptions",
        "schedule_mode IS NULL OR schedule_mode IN ('interval', 'fixed_time', 'manual')",
    )
    op.create_check_constraint(
        "ck_subscriptions_schedule_sync_consistent",
        "subscriptions",
        "(schedule_mode = 'manual' AND sync_enabled IS FALSE) OR "
        "(schedule_mode IS DISTINCT FROM 'manual' AND sync_enabled IS TRUE)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_subscriptions_schedule_sync_consistent",
        "subscriptions",
        type_="check",
    )
    op.drop_constraint(
        "ck_subscriptions_schedule_mode",
        "subscriptions",
        type_="check",
    )
