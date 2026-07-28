"""Add Task Engine fields to download_jobs and import_jobs

Revision ID: a0b1c2d3e4f5
Revises: f6a7b8c9d0e1
Create Date: 2026-06-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a0b1c2d3e4f5"
down_revision: Union[str, None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── download_jobs ──
    result = conn.execute(sa.text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='download_jobs' AND column_name='priority'"
    ))
    if result.scalar() is None:
        op.add_column("download_jobs", sa.Column("priority", sa.Integer(), nullable=False, server_default="10"))
        op.add_column("download_jobs", sa.Column("user_note", sa.Text(), nullable=True))
        op.add_column("download_jobs", sa.Column("operator_name", sa.String(100), nullable=True))
        op.add_column("download_jobs", sa.Column("operator_action", sa.String(50), nullable=True))
        op.add_column("download_jobs", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column("download_jobs", sa.Column("worker_pid", sa.Integer(), nullable=True))
        op.add_column("download_jobs", sa.Column("pipeline_stage", sa.String(50), nullable=True))
        op.add_column("download_jobs", sa.Column("progress_data", sa.dialects.postgresql.JSONB(), nullable=True))

    # ── import_jobs ──
    result = conn.execute(sa.text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='import_jobs' AND column_name='priority'"
    ))
    if result.scalar() is None:
        op.add_column("import_jobs", sa.Column("priority", sa.Integer(), nullable=False, server_default="10"))
        op.add_column("import_jobs", sa.Column("user_note", sa.Text(), nullable=True))
        op.add_column("import_jobs", sa.Column("operator_name", sa.String(100), nullable=True))
        op.add_column("import_jobs", sa.Column("operator_action", sa.String(50), nullable=True))
        op.add_column("import_jobs", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column("import_jobs", sa.Column("worker_pid", sa.Integer(), nullable=True))
        op.add_column("import_jobs", sa.Column("import_retry_count", sa.Integer(), nullable=False, server_default="0"))
        op.add_column("import_jobs", sa.Column("max_import_retries", sa.Integer(), nullable=False, server_default="3"))
        op.add_column("import_jobs", sa.Column("progress_stage", sa.String(50), nullable=True))
        op.add_column("import_jobs", sa.Column("progress_works_done", sa.Integer(), nullable=True))
        op.add_column("import_jobs", sa.Column("progress_works_total", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("download_jobs", "progress_data")
    op.drop_column("download_jobs", "pipeline_stage")
    op.drop_column("download_jobs", "worker_pid")
    op.drop_column("download_jobs", "last_heartbeat_at")
    op.drop_column("download_jobs", "operator_action")
    op.drop_column("download_jobs", "operator_name")
    op.drop_column("download_jobs", "user_note")
    op.drop_column("download_jobs", "priority")

    op.drop_column("import_jobs", "progress_works_total")
    op.drop_column("import_jobs", "progress_works_done")
    op.drop_column("import_jobs", "progress_stage")
    op.drop_column("import_jobs", "max_import_retries")
    op.drop_column("import_jobs", "import_retry_count")
    op.drop_column("import_jobs", "worker_pid")
    op.drop_column("import_jobs", "last_heartbeat_at")
    op.drop_column("import_jobs", "operator_action")
    op.drop_column("import_jobs", "operator_name")
    op.drop_column("import_jobs", "user_note")
    op.drop_column("import_jobs", "priority")
