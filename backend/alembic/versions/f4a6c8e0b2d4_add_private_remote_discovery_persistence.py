"""add private remote discovery persistence

Revision ID: f4a6c8e0b2d4
Revises: b3d5f7a9c1e4
Create Date: 2026-08-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f4a6c8e0b2d4"
down_revision: Union[str, None] = "b3d5f7a9c1e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sync_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sync_interval_hours", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("schedule_mode", sa.String(length=20), nullable=True),
        sa.Column("schedule_rule", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("scheduled_times", sa.String(length=100), nullable=True),
        sa.CheckConstraint(
            "schedule_mode IS NULL OR schedule_mode IN ('interval', 'calendar', 'manual')",
            name="ck_user_subscriptions_schedule_mode",
        ),
        sa.CheckConstraint(
            "(schedule_mode = 'manual' AND sync_enabled IS FALSE) OR "
            "(schedule_mode IS DISTINCT FROM 'manual' AND sync_enabled IS TRUE)",
            name="ck_user_subscriptions_schedule_sync_consistent",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_user_subscriptions_user", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], name="fk_user_subscriptions_subscription", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "subscription_id", name="uq_user_subscriptions_user_subscription"),
        sa.UniqueConstraint("id", "user_id", "subscription_id", name="uq_user_subscriptions_id_owner_subscription"),
    )
    op.create_table(
        "remote_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("remote_user_id", sa.String(length=255), nullable=True),
        sa.Column("remote_username", sa.String(length=255), nullable=True),
        sa.Column("auth_method", sa.String(length=50), nullable=True),
        sa.Column(
            "scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "collection_selectors",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("credential_ciphertext", sa.Text(), nullable=True),
        sa.Column("credential_key_version", sa.Integer(), nullable=True),
        sa.Column("credential_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("auth_status", sa.String(length=30), nullable=True),
        sa.Column("auth_error_reason", sa.Text(), nullable=True),
        sa.Column("last_authenticated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scan_cursor", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_scan_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scan_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scan_interval_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("auto_import_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto_import_min_confidence", sa.String(length=20), nullable=False, server_default="high"),
        sa.Column("auto_import_limit", sa.Integer(), nullable=False, server_default="25"),
        sa.CheckConstraint("source IN ('pixiv', 'x', 'bilibili')", name="ck_remote_accounts_source"),
        sa.CheckConstraint(
            "auth_method IS NULL OR "
            "(source = 'pixiv' AND auth_method = 'refresh_token') OR "
            "(source = 'x' AND auth_method IN ('oauth2', 'cookie')) OR "
            "(source = 'bilibili' AND auth_method = 'sessdata')",
            name="ck_remote_accounts_auth_method",
        ),
        sa.CheckConstraint(
            "auto_import_min_confidence IN ('high', 'medium', 'low')",
            name="ck_remote_accounts_auto_import_confidence",
        ),
        sa.CheckConstraint("auto_import_limit BETWEEN 1 AND 200", name="ck_remote_accounts_auto_import_limit"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_remote_accounts_user", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "source", name="uq_remote_accounts_user_source"),
        sa.UniqueConstraint("id", "user_id", name="uq_remote_accounts_id_owner"),
    )
    op.create_index(
        "ix_remote_accounts_next_scan_due",
        "remote_accounts",
        ["next_scan_at", "id"],
        postgresql_where=sa.text("is_enabled IS TRUE"),
    )
    op.create_unique_constraint(
        "uq_subscription_sources_id_subscription",
        "subscription_sources",
        ["id", "subscription_id"],
    )
    op.create_table(
        "user_subscription_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("user_subscription_id", sa.Uuid(), nullable=False),
        sa.Column("subscription_source_id", sa.Uuid(), nullable=False),
        sa.Column("remote_account_id", sa.Uuid(), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_successful_auth", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auth_healthy", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auth_status", sa.String(length=30), nullable=True),
        sa.Column("auth_error_reason", sa.Text(), nullable=True),
        sa.Column("last_auth_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_subscription_id"], ["user_subscriptions.id"], name="fk_user_subscription_sources_membership", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_user_subscription_sources_user", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], name="fk_user_subscription_sources_subscription", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["subscription_source_id"], ["subscription_sources.id"], name="fk_user_subscription_sources_source", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["remote_account_id"], ["remote_accounts.id"], name="fk_user_subscription_sources_remote_account", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["user_subscription_id", "user_id", "subscription_id"],
            ["user_subscriptions.id", "user_subscriptions.user_id", "user_subscriptions.subscription_id"],
            name="fk_user_subscription_sources_membership_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_source_id", "subscription_id"],
            ["subscription_sources.id", "subscription_sources.subscription_id"],
            name="fk_user_subscription_sources_source_subscription",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["remote_account_id", "user_id"],
            ["remote_accounts.id", "remote_accounts.user_id"],
            name="fk_user_subscription_sources_remote_account_owner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_subscription_id",
            "subscription_source_id",
            name="uq_user_subscription_sources_membership_source",
        ),
    )
    op.create_index(
        "ix_user_subscription_sources_next_sync_due",
        "user_subscription_sources",
        ["next_sync_at", "id"],
        postgresql_where=sa.text("is_enabled IS TRUE AND auth_healthy IS TRUE"),
    )
    op.create_table(
        "discovery_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("remote_account_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("source_creator_id", sa.String(length=255), nullable=False),
        sa.Column("remote_url", sa.String(length=2000), nullable=True),
        sa.Column("display_name", sa.String(length=500), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("confidence", sa.String(length=20), nullable=False, server_default="low"),
        sa.Column("confidence_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("subscription_id", sa.Uuid(), nullable=True),
        sa.Column("user_subscription_id", sa.Uuid(), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_following", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.CheckConstraint("confidence IN ('high', 'medium', 'low')", name="ck_discovery_candidates_confidence"),
        sa.CheckConstraint(
            "state IN ('pending', 'dismissed', 'imported', 'conflict')", name="ck_discovery_candidates_state"
        ),
        sa.CheckConstraint(
            "user_subscription_id IS NULL OR subscription_id IS NOT NULL",
            name="ck_discovery_candidates_membership_subscription",
        ),
        sa.ForeignKeyConstraint(
            ["remote_account_id"], ["remote_accounts.id"], name="fk_discovery_candidates_remote_account", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_discovery_candidates_user", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], name="fk_discovery_candidates_subscription", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["user_subscription_id"], ["user_subscriptions.id"], name="fk_discovery_candidates_membership", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["remote_account_id", "user_id"],
            ["remote_accounts.id", "remote_accounts.user_id"],
            name="fk_discovery_candidates_account_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_subscription_id", "user_id", "subscription_id"],
            ["user_subscriptions.id", "user_subscriptions.user_id", "user_subscriptions.subscription_id"],
            name="fk_discovery_candidates_membership_owner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("remote_account_id", "source_creator_id", name="uq_discovery_candidates_account_creator"),
    )
    op.create_index("ix_discovery_candidates_account_state", "discovery_candidates", ["remote_account_id", "state", "id"])

    op.add_column("download_jobs", sa.Column("triggering_user_subscription_id", sa.Uuid(), nullable=True))
    op.add_column("download_jobs", sa.Column("triggering_remote_account_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_download_jobs_triggering_membership",
        "download_jobs",
        "user_subscriptions",
        ["triggering_user_subscription_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_download_jobs_triggering_remote_account",
        "download_jobs",
        "remote_accounts",
        ["triggering_remote_account_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("task_runs", sa.Column("triggering_user_subscription_id", sa.Uuid(), nullable=True))
    op.add_column("task_runs", sa.Column("triggering_remote_account_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_task_runs_triggering_membership",
        "task_runs",
        "user_subscriptions",
        ["triggering_user_subscription_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_task_runs_triggering_remote_account",
        "task_runs",
        "remote_accounts",
        ["triggering_remote_account_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Legacy canonical rows remain in place. Their earliest active administrator
    # receives a private membership/source policy with no remote account or
    # credentials, so current installations keep the same effective behavior.
    op.execute(
        """
        INSERT INTO user_subscriptions (
            id, user_id, subscription_id,
            name, is_active, sync_enabled, sync_interval_hours, schedule_mode,
            schedule_rule, scheduled_times,
            created_at, updated_at
        )
        SELECT
            gen_random_uuid(), admin.id, subscription.id,
            subscription.name, subscription.is_active, subscription.sync_enabled,
            subscription.sync_interval_hours, subscription.schedule_mode,
            subscription.schedule_rule, subscription.scheduled_times,
            now(), now()
        FROM subscriptions AS subscription
        CROSS JOIN LATERAL (
            SELECT id
            FROM users
            WHERE is_active IS TRUE AND is_admin IS TRUE
            ORDER BY created_at ASC, id ASC
            LIMIT 1
        ) AS admin
        ON CONFLICT (user_id, subscription_id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO user_subscription_sources (
            id, user_id, subscription_id, user_subscription_id, subscription_source_id, is_enabled,
            last_successful_auth, auth_healthy, last_synced_at, last_attempted_at,
            next_sync_at, auth_status, auth_error_reason, last_auth_checked_at,
            created_at, updated_at
        )
        SELECT
            gen_random_uuid(), membership.user_id, membership.subscription_id,
            membership.id, source.id, source.is_enabled,
            source.last_successful_auth, source.auth_healthy, source.last_synced_at,
            source.last_attempted_at, source.next_sync_at, source.auth_status,
            source.auth_error_reason, source.last_auth_checked_at, now(), now()
        FROM user_subscriptions AS membership
        JOIN subscription_sources AS source ON source.subscription_id = membership.subscription_id
        WHERE membership.user_id = (
            SELECT id
            FROM users
            WHERE is_active IS TRUE AND is_admin IS TRUE
            ORDER BY created_at ASC, id ASC
            LIMIT 1
        )
        ON CONFLICT (user_subscription_id, subscription_source_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_constraint("fk_task_runs_triggering_remote_account", "task_runs", type_="foreignkey")
    op.drop_constraint("fk_task_runs_triggering_membership", "task_runs", type_="foreignkey")
    op.drop_column("task_runs", "triggering_remote_account_id")
    op.drop_column("task_runs", "triggering_user_subscription_id")
    op.drop_constraint("fk_download_jobs_triggering_remote_account", "download_jobs", type_="foreignkey")
    op.drop_constraint("fk_download_jobs_triggering_membership", "download_jobs", type_="foreignkey")
    op.drop_column("download_jobs", "triggering_remote_account_id")
    op.drop_column("download_jobs", "triggering_user_subscription_id")
    op.drop_index("ix_discovery_candidates_account_state", table_name="discovery_candidates")
    op.drop_table("discovery_candidates")
    op.drop_index("ix_user_subscription_sources_next_sync_due", table_name="user_subscription_sources")
    op.drop_table("user_subscription_sources")
    op.drop_index("ix_remote_accounts_next_scan_due", table_name="remote_accounts")
    op.drop_table("remote_accounts")
    op.drop_constraint("uq_subscription_sources_id_subscription", "subscription_sources", type_="unique")
    op.drop_table("user_subscriptions")
