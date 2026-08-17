"""add structured calendar subscription rules

Revision ID: a7c9e1f3b5d7
Revises: f4c6d8e0a2b3
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a7c9e1f3b5d7"
down_revision: Union[str, None] = "f4c6d8e0a2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("schedule_rule", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.drop_constraint("ck_subscriptions_schedule_mode", "subscriptions", type_="check")
    op.execute("""
        UPDATE subscriptions
        SET schedule_rule = jsonb_build_object(
                'frequency', 'daily',
                'times', to_jsonb(regexp_split_to_array(regexp_replace(scheduled_times, '\\s+', '', 'g'), ','))
            ),
            schedule_mode = 'calendar'
        WHERE schedule_mode = 'fixed_time'
          AND NULLIF(btrim(scheduled_times), '') IS NOT NULL
    """)
    op.execute("""
        UPDATE subscriptions
        SET schedule_mode = 'interval', schedule_rule = NULL
        WHERE schedule_mode = 'fixed_time'
    """)
    op.execute("""
        UPDATE system_settings
        SET value = value || jsonb_build_object(
            'schedule_mode', 'calendar',
            'schedule_rule', jsonb_build_object(
                'frequency', 'daily',
                'times', to_jsonb(regexp_split_to_array(
                    regexp_replace(value->>'scheduled_times', '\\s+', '', 'g'), ','
                ))
            )
        )
        WHERE key = 'subscription_defaults'
          AND value->>'schedule_mode' = 'fixed_time'
          AND NULLIF(btrim(value->>'scheduled_times'), '') IS NOT NULL
    """)
    op.execute("""
        UPDATE system_settings
        SET value = value || jsonb_build_object(
            'schedule_mode', 'interval',
            'schedule_rule', NULL
        )
        WHERE key = 'subscription_defaults'
          AND value->>'schedule_mode' = 'fixed_time'
    """)
    op.create_check_constraint(
        "ck_subscriptions_schedule_mode",
        "subscriptions",
        "schedule_mode IS NULL OR schedule_mode IN ('interval', 'calendar', 'manual')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_subscriptions_schedule_mode", "subscriptions", type_="check")
    op.execute("""
        UPDATE subscriptions
        SET scheduled_times = (
                SELECT string_agg(value, ',' ORDER BY value)
                FROM jsonb_array_elements_text(schedule_rule->'times') AS value
            ),
            schedule_mode = 'fixed_time'
        WHERE schedule_mode = 'calendar'
    """)
    op.execute("""
        UPDATE system_settings
        SET value = value || jsonb_build_object(
            'schedule_mode', 'fixed_time',
            'scheduled_times', COALESCE((
                SELECT string_agg(item, ',' ORDER BY item)
                FROM jsonb_array_elements_text(value->'schedule_rule'->'times') AS item
            ), '')
        ) - 'schedule_rule'
        WHERE key = 'subscription_defaults'
          AND value->>'schedule_mode' = 'calendar'
    """)
    op.create_check_constraint(
        "ck_subscriptions_schedule_mode",
        "subscriptions",
        "schedule_mode IS NULL OR schedule_mode IN ('interval', 'fixed_time', 'manual')",
    )
    op.drop_column("subscriptions", "schedule_rule")
