"""Durable global scheduler batch identities and source outcomes."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "fa02b4c6d8e0"
down_revision = "f9e1a3b5c7d9"
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade():
    op.create_table(
        "scheduler_batches",
        *timestamps(),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("actor_user_id", sa.Integer()),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("initialized_at", sa.DateTime(timezone=True)),
        sa.Column("legacy_task_id", sa.Uuid()),
        sa.Column("result", postgresql.JSONB()),
        sa.UniqueConstraint("request_id", name="uq_scheduler_batch_request"),
        sa.UniqueConstraint("task_id", name="uq_scheduler_batch_task"),
    )
    op.create_index("uq_scheduler_batch_active", "scheduler_batches", [sa.text("(true)")], unique=True, postgresql_where=sa.text("state = 'active'"))
    op.create_table(
        "scheduler_batch_items",
        *timestamps(),
        sa.Column("batch_id", sa.Uuid(), sa.ForeignKey("scheduler_batches.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(50)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("download_job_id", sa.Uuid()),
        sa.Column("child_task_id", sa.Uuid()),
        sa.Column("owns_download", sa.Boolean(), nullable=False),
        sa.Column("reason_code", sa.String(80)),
        sa.Column("error", sa.Text()),
        sa.Column("outcome", postgresql.JSONB()),
        sa.UniqueConstraint("batch_id", "source_id", name="uq_scheduler_batch_source"),
    )
    op.create_index("ix_scheduler_batch_item_due", "scheduler_batch_items", ["batch_id", "next_retry_at", "id"])
    op.create_index("ix_scheduler_batch_item_download", "scheduler_batch_items", ["download_job_id"])


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM scheduler_batches WHERE state = 'active')
        THEN RAISE EXCEPTION 'Drain scheduler batches before downgrade'; END IF; END $$""")
    op.drop_table("scheduler_batch_items")
    op.drop_table("scheduler_batches")
