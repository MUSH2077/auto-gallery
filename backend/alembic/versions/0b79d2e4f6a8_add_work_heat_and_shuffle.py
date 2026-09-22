"""add work heat, stable shuffle, and source ranking snapshots

Revision ID: 0b79d2e4f6a8
Revises: ff57a91bcd35
Create Date: 2026-09-22
"""

from alembic import op
import sqlalchemy as sa


revision: str = "0b79d2e4f6a8"
down_revision: str = "ff57a91bcd35"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "works",
        sa.Column(
            "shuffle_key",
            sa.BigInteger(),
            sa.Computed(
                "((('x' || substr(md5(id::text), 1, 16))::bit(64)::bigint) "
                "& 9223372036854775807)",
                persisted=True,
            ),
            nullable=False,
        ),
    )
    op.add_column("works", sa.Column("heat_score", sa.Float(), nullable=True))
    op.add_column("works", sa.Column("heat_observed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_works_heat_score_id", "works", ["heat_score", "id"])
    op.create_index("ix_works_shuffle_key_id", "works", ["shuffle_key", "id"])

    op.add_column("work_sources", sa.Column("engagement_count", sa.BigInteger(), nullable=True))
    op.add_column("work_sources", sa.Column("view_count", sa.BigInteger(), nullable=True))
    op.add_column("work_sources", sa.Column("metrics_observed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("work_sources", sa.Column("source_heat_score", sa.Float(), nullable=True))
    op.add_column("work_sources", sa.Column("heat_basis", sa.String(length=32), nullable=True))

    op.create_table(
        "source_ranking_snapshots",
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("mode", sa.String(length=50), nullable=False),
        sa.Column("ranking_date", sa.Date(), nullable=False),
        sa.Column("source_work_id", sa.String(length=255), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("rank_total", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source",
            "mode",
            "ranking_date",
            "source_work_id",
            name="uq_source_ranking_snapshot_entry",
        ),
    )
    op.create_index(
        "ix_source_ranking_snapshots_current",
        "source_ranking_snapshots",
        ["source", "fetched_at", "source_work_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_source_ranking_snapshots_current", table_name="source_ranking_snapshots")
    op.drop_table("source_ranking_snapshots")
    for column in (
        "heat_basis",
        "source_heat_score",
        "metrics_observed_at",
        "view_count",
        "engagement_count",
    ):
        op.drop_column("work_sources", column)
    op.drop_index("ix_works_shuffle_key_id", table_name="works")
    op.drop_index("ix_works_heat_score_id", table_name="works")
    op.drop_column("works", "heat_observed_at")
    op.drop_column("works", "heat_score")
    op.drop_column("works", "shuffle_key")
