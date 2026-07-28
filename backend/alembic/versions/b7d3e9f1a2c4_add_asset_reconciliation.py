"""add asset-level visual reconciliation

Revision ID: b7d3e9f1a2c4
Revises: a1f2b3c4d5e7
Create Date: 2026-07-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "b7d3e9f1a2c4"
down_revision: Union[str, None] = "a1f2b3c4d5e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE system_settings
        SET value = (
            value
            - 'source_level_enabled'
            - 'cross_source_enabled'
            - 'auto_merge'
        ) || jsonb_build_object(
            'auto_group_enabled',
            COALESCE(
                (value ->> 'auto_group_enabled')::boolean,
                (value ->> 'auto_merge')::boolean,
                true
            ),
            'phash_threshold',
            LEAST(4, GREATEST(0, COALESCE((value ->> 'phash_threshold')::integer, 4))),
            'ssim_threshold',
            COALESCE((value -> 'ssim_threshold'), to_jsonb(0.98)),
            'aspect_ratio_tolerance',
            COALESCE((value -> 'aspect_ratio_tolerance'), to_jsonb(0.01)),
            'auto_group_score',
            COALESCE((value -> 'auto_group_score'), to_jsonb(95)),
            'review_score',
            COALESCE((value -> 'review_score'), to_jsonb(70)),
            'quarantine_days',
            COALESCE((value -> 'quarantine_days'), to_jsonb(30))
        )
        WHERE key = 'dedup' AND jsonb_typeof(value) = 'object'
        """
    )

    op.add_column("asset_sources", sa.Column("ordinal", sa.Integer(), nullable=True))
    op.add_column(
        "asset_sources",
        sa.Column("role", sa.String(length=30), nullable=False, server_default="page"),
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY work_source_id
                       ORDER BY created_at, id
                   ) - 1 AS ordinal
            FROM asset_sources
            WHERE work_source_id IS NOT NULL
        )
        UPDATE asset_sources AS target
        SET ordinal = ranked.ordinal
        FROM ranked
        WHERE target.id = ranked.id
        """
    )
    op.create_index(
        "uq_asset_sources_work_ordinal_role",
        "asset_sources",
        ["work_source_id", "ordinal", "role"],
        unique=True,
        postgresql_where=sa.text("work_source_id IS NOT NULL AND ordinal IS NOT NULL"),
    )

    op.add_column(
        "asset_storage_states",
        sa.Column("served_by_asset_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "asset_storage_states",
        sa.Column("dedup_kind", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "asset_storage_states",
        sa.Column("quarantine_path", sa.String(length=2000), nullable=True),
    )
    op.add_column(
        "asset_storage_states",
        sa.Column("quarantine_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "asset_storage_states",
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "asset_storage_states",
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "asset_storage_states",
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_asset_storage_states_served_by_asset",
        "asset_storage_states",
        "assets",
        ["served_by_asset_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "asset_dedup_evidence",
        sa.Column("left_asset_id", sa.Uuid(), nullable=False),
        sa.Column("right_asset_id", sa.Uuid(), nullable=False),
        sa.Column("algorithm_version", sa.String(length=50), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("sha256_equal", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("phash_distance", sa.Integer(), nullable=True),
        sa.Column("ssim_score", sa.Float(), nullable=True),
        sa.Column("aspect_ratio_delta", sa.Float(), nullable=True),
        sa.Column("visual_score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("metadata_score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("hard_gate_passed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("facts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("left_asset_id <> right_asset_id", name="ck_asset_dedup_evidence_distinct"),
        sa.CheckConstraint("left_asset_id < right_asset_id", name="ck_asset_dedup_evidence_ordered"),
        sa.ForeignKeyConstraint(["left_asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["right_asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "left_asset_id",
            "right_asset_id",
            "algorithm_version",
            "input_digest",
            name="uq_asset_dedup_evidence_pair_revision",
        ),
    )
    op.create_index(
        "ix_asset_dedup_evidence_pair",
        "asset_dedup_evidence",
        ["left_asset_id", "right_asset_id", "created_at"],
    )

    op.create_table(
        "visual_asset_groups",
        sa.Column("representative_asset_id", sa.Uuid(), nullable=False),
        sa.Column("policy_version", sa.String(length=50), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["representative_asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_visual_asset_groups_representative",
        "visual_asset_groups",
        ["representative_asset_id"],
    )

    op.create_table(
        "visual_asset_members",
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("quality_facts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["evidence_id"], ["asset_dedup_evidence.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["group_id"], ["visual_asset_groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id", name="uq_visual_asset_members_asset"),
        sa.UniqueConstraint("group_id", "asset_id", name="uq_visual_asset_members_group_asset"),
    )
    op.create_index("ix_visual_asset_members_group", "visual_asset_members", ["group_id"])

    op.create_table(
        "asset_dedup_cases",
        sa.Column("left_asset_id", sa.Uuid(), nullable=False),
        sa.Column("right_asset_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("suggested_representative_asset_id", sa.Uuid(), nullable=True),
        sa.Column("decided_by", sa.String(length=255), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("left_asset_id <> right_asset_id", name="ck_asset_dedup_cases_distinct"),
        sa.CheckConstraint("left_asset_id < right_asset_id", name="ck_asset_dedup_cases_ordered"),
        sa.CheckConstraint(
            "status IN ('pending', 'merged', 'separate', 'deferred')",
            name="ck_asset_dedup_cases_status",
        ),
        sa.ForeignKeyConstraint(["evidence_id"], ["asset_dedup_evidence.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["left_asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["right_asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["suggested_representative_asset_id"], ["assets.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("left_asset_id", "right_asset_id", name="uq_asset_dedup_cases_pair"),
    )
    op.create_index(
        "ix_asset_dedup_cases_queue",
        "asset_dedup_cases",
        ["status", "created_at", "id"],
    )

    op.create_table(
        "asset_dedup_decisions",
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("actor_type", sa.String(length=30), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=True),
        sa.Column("representative_asset_id", sa.Uuid(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("curation_commit_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "action IN ('merge', 'separate', 'defer')",
            name="ck_asset_dedup_decisions_action",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["asset_dedup_cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["curation_commit_id"], ["curation_commits.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["representative_asset_id"], ["assets.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_asset_dedup_decisions_case",
        "asset_dedup_decisions",
        ["case_id", "created_at"],
    )

    op.create_table(
        "asset_dedup_outbox",
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('hardlink', 'quarantine', 'purge')",
            name="ck_asset_dedup_outbox_event_type",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'processing', 'complete', 'failed')",
            name="ck_asset_dedup_outbox_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_asset_dedup_outbox_ready",
        "asset_dedup_outbox",
        ["state", "available_at", "id"],
    )
    op.create_index(
        "ix_asset_dedup_outbox_processing_lease",
        "asset_dedup_outbox",
        ["state", "updated_at", "id"],
        postgresql_where=sa.text("state = 'processing'"),
    )

    op.create_table(
        "asset_dedup_scans",
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("cursor_asset_id", sa.Uuid(), nullable=True),
        sa.Column("assets_scanned", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("candidates_evaluated", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cases_created", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("assets_grouped", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("bytes_reclaimable", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("options", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'complete', 'failed')",
            name="ck_asset_dedup_scans_status",
        ),
        sa.ForeignKeyConstraint(["cursor_asset_id"], ["assets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_asset_dedup_scans_status", "asset_dedup_scans", ["status"])

    op.create_index(
        "ix_assets_sha256_present",
        "assets",
        ["sha256"],
        postgresql_where=sa.text("sha256 IS NOT NULL"),
    )
    op.create_index(
        "ix_assets_phash_present",
        "assets",
        ["phash"],
        postgresql_where=sa.text("phash IS NOT NULL"),
    )
    for index, (start, length) in enumerate(((1, 3), (4, 3), (7, 3), (10, 3), (13, 4))):
        op.execute(
            f"CREATE INDEX ix_assets_phash_band_{index} "
            f"ON assets ((substr(phash, {start}, {length}))) "
            "WHERE phash IS NOT NULL"
        )


def downgrade() -> None:
    for index in range(5):
        op.drop_index(f"ix_assets_phash_band_{index}", table_name="assets")
    op.drop_index("ix_assets_phash_present", table_name="assets")
    op.drop_index("ix_assets_sha256_present", table_name="assets")

    op.drop_index("ix_asset_dedup_scans_status", table_name="asset_dedup_scans")
    op.drop_table("asset_dedup_scans")
    op.drop_index(
        "ix_asset_dedup_outbox_processing_lease",
        table_name="asset_dedup_outbox",
    )
    op.drop_index("ix_asset_dedup_outbox_ready", table_name="asset_dedup_outbox")
    op.drop_table("asset_dedup_outbox")
    op.drop_index("ix_asset_dedup_decisions_case", table_name="asset_dedup_decisions")
    op.drop_table("asset_dedup_decisions")
    op.drop_index("ix_asset_dedup_cases_queue", table_name="asset_dedup_cases")
    op.drop_table("asset_dedup_cases")
    op.drop_index("ix_visual_asset_members_group", table_name="visual_asset_members")
    op.drop_table("visual_asset_members")
    op.drop_index("ix_visual_asset_groups_representative", table_name="visual_asset_groups")
    op.drop_table("visual_asset_groups")
    op.drop_index("ix_asset_dedup_evidence_pair", table_name="asset_dedup_evidence")
    op.drop_table("asset_dedup_evidence")

    op.drop_constraint(
        "fk_asset_storage_states_served_by_asset",
        "asset_storage_states",
        type_="foreignkey",
    )
    op.drop_column("asset_storage_states", "last_verified_at")
    op.drop_column("asset_storage_states", "purge_after")
    op.drop_column("asset_storage_states", "quarantined_at")
    op.drop_column("asset_storage_states", "quarantine_sha256")
    op.drop_column("asset_storage_states", "quarantine_path")
    op.drop_column("asset_storage_states", "dedup_kind")
    op.drop_column("asset_storage_states", "served_by_asset_id")

    op.drop_index("uq_asset_sources_work_ordinal_role", table_name="asset_sources")
    op.drop_column("asset_sources", "role")
    op.drop_column("asset_sources", "ordinal")

    op.execute(
        """
        UPDATE system_settings
        SET value = (
            value
            - 'auto_group_enabled'
            - 'ssim_threshold'
            - 'aspect_ratio_tolerance'
            - 'auto_group_score'
            - 'review_score'
            - 'quarantine_days'
        ) || jsonb_build_object(
            'source_level_enabled', false,
            'cross_source_enabled', false,
            'auto_merge', COALESCE((value ->> 'auto_group_enabled')::boolean, false),
            'phash_threshold', COALESCE((value -> 'phash_threshold'), to_jsonb(4))
        )
        WHERE key = 'dedup' AND jsonb_typeof(value) = 'object'
        """
    )
