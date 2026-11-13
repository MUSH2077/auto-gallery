"""Add PostgreSQL search generations and pin durable commands to their backend.

Revision ID: 026acd31ef57
Revises: 0159bc20de46
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "026acd31ef57"
down_revision = "0159bc20de46"
branch_labels = None
depends_on = None


def upgrade():
    # Extensions may be shared with other features; downgrade leaves them intact.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS fuzzystrmatch")
    op.create_table("search_pg_indexes",
        sa.Column("uid", sa.Text(), primary_key=True),
        sa.Column("storage_id", sa.Uuid(), nullable=False),
        sa.Column("settings", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.create_table("search_pg_documents",
        sa.Column("storage_id", sa.Uuid(), primary_key=True),
        sa.Column("entity_id", sa.Text(), primary_key=True),
        sa.Column("document", postgresql.JSONB(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("tokens", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=False))
    op.create_index("ix_search_pg_documents_vector", "search_pg_documents", ["search_vector"], postgresql_using="gin")
    op.create_index("ix_search_pg_documents_tokens", "search_pg_documents", ["tokens"], postgresql_using="gin")
    op.create_index("ix_search_pg_documents_text", "search_pg_documents", ["search_text"], postgresql_using="gin", postgresql_ops={"search_text": "gin_trgm_ops"})
    op.create_table("search_pg_tasks",
        sa.Column("uid", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("index_uid", sa.Text()), sa.Column("type", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("details", postgresql.JSONB()), sa.Column("error", postgresql.JSONB()))
    op.create_index("ix_search_pg_tasks_finished_at", "search_pg_tasks", ["finished_at"])
    for table in ("search_delivery_receipts", "search_rebuilds"):
        op.add_column(table, sa.Column("backend", sa.String(16), nullable=False, server_default="meilisearch"))


def downgrade():
    for table in ("search_rebuilds", "search_delivery_receipts"):
        op.drop_column(table, "backend")
    op.drop_table("search_pg_tasks")
    op.drop_table("search_pg_documents")
    op.drop_table("search_pg_indexes")
