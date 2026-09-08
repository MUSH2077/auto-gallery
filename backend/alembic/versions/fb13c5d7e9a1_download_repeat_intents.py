"""Durable actor-bound completed-sync repeat receipts."""

from alembic import op
import sqlalchemy as sa

revision = "fb13c5d7e9a1"
down_revision = "fa02b4c6d8e0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "download_repeat_intents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("previous_job_id", sa.Uuid(), nullable=False),
        sa.Column("previous_task_id", sa.Uuid(), nullable=False),
        sa.Column("download_job_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.UniqueConstraint("actor_user_id", "request_id", name="uq_download_repeat_actor_request"),
    )


def downgrade():
    op.drop_table("download_repeat_intents")
