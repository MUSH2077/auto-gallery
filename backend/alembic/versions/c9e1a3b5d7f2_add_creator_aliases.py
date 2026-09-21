"""add durable creator identity aliases

Revision ID: c9e1a3b5d7f2
Revises: b8d0f2a4c6e9
"""

from alembic import op
import sqlalchemy as sa


revision = "c9e1a3b5d7f2"
down_revision = "b8d0f2a4c6e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "creator_aliases",
        sa.Column("creator_id", sa.Uuid(), nullable=False),
        sa.Column("value", sa.String(length=2000), nullable=False),
        sa.Column("normalized_value", sa.String(length=2000), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("source_ref", sa.String(length=2000), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["creator_id"],
            ["creators.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "creator_id",
            "source",
            "kind",
            "normalized_value",
            name="uq_creator_aliases_identity",
        ),
    )
    op.create_index(
        "ix_creator_aliases_normalized_value",
        "creator_aliases",
        ["normalized_value"],
    )
    op.create_index(
        "ix_creator_aliases_creator_current",
        "creator_aliases",
        ["creator_id", "is_current"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_creator_aliases_creator_current",
        table_name="creator_aliases",
    )
    op.drop_index(
        "ix_creator_aliases_normalized_value",
        table_name="creator_aliases",
    )
    op.drop_table("creator_aliases")
