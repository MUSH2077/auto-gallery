"""fix work posted_at column types

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-06-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_type(table: str, column: str) -> str | None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_name = :table AND column_name = :column
            """
        ),
        {"table": table, "column": column},
    )
    return result.scalar()


def _fix_posted_at(table: str) -> None:
    current_type = _column_type(table, "posted_at")
    if current_type and "timestamp" in current_type:
        return
    if current_type is None:
        return

    op.execute(
        sa.text(
            f"""
            ALTER TABLE {table}
            ALTER COLUMN posted_at TYPE timestamp with time zone
            USING CASE
                WHEN posted_at IS NULL OR btrim(posted_at) = '' THEN NULL
                WHEN posted_at ~ '^[0-9]{{13}}$'
                    THEN to_timestamp((posted_at::double precision) / 1000.0)
                WHEN posted_at ~ '^[0-9]{{10}}(\\.[0-9]+)?$'
                    THEN to_timestamp(posted_at::double precision)
                WHEN posted_at ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'
                    THEN (posted_at::date)::timestamp with time zone
                WHEN posted_at ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}[ T][0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}}'
                    THEN replace(posted_at, 'Z', '+00:00')::timestamp with time zone
                ELSE NULL
            END
            """
        )
    )


def upgrade() -> None:
    _fix_posted_at("works")
    _fix_posted_at("work_sources")


def downgrade() -> None:
    op.alter_column(
        "work_sources",
        "posted_at",
        type_=sa.String(length=100),
        existing_type=sa.DateTime(timezone=True),
        postgresql_using="posted_at::text",
    )
    op.alter_column(
        "works",
        "posted_at",
        type_=sa.String(length=100),
        existing_type=sa.DateTime(timezone=True),
        postgresql_using="posted_at::text",
    )
