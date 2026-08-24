"""repair and normalize structured calendar schedule rules

Revision ID: b3d5f7a9c1e4
Revises: a7c9e1f3b5d7
"""

from alembic import op


revision = "b3d5f7a9c1e4"
down_revision = "a7c9e1f3b5d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Some databases were stamped at the previous published head without the
    # column physically present. Alembic will not replay a stamped revision, so
    # repair that drift in a new revision rather than editing migration history.
    op.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS schedule_rule JSONB")
    op.execute("""
        UPDATE subscriptions
        SET schedule_rule = jsonb_set(
            schedule_rule,
            '{times}',
            COALESCE((
                SELECT jsonb_agg(btrim(item.value) ORDER BY item.ordinality)
                FROM jsonb_array_elements_text(schedule_rule->'times')
                    WITH ORDINALITY AS item(value, ordinality)
            ), '[]'::jsonb),
            FALSE
        )
        WHERE jsonb_typeof(schedule_rule) = 'object'
          AND jsonb_typeof(schedule_rule->'times') = 'array'
          AND EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(schedule_rule->'times') AS item(value)
              WHERE item.value IS DISTINCT FROM btrim(item.value)
          )
    """)
    op.execute("""
        UPDATE system_settings
        SET value = jsonb_set(
            value,
            '{schedule_rule,times}',
            COALESCE((
                SELECT jsonb_agg(btrim(item.value) ORDER BY item.ordinality)
                FROM jsonb_array_elements_text(value->'schedule_rule'->'times')
                    WITH ORDINALITY AS item(value, ordinality)
            ), '[]'::jsonb),
            FALSE
        )
        WHERE key = 'subscription_defaults'
          AND jsonb_typeof(value) = 'object'
          AND jsonb_typeof(value->'schedule_rule') = 'object'
          AND jsonb_typeof(value->'schedule_rule'->'times') = 'array'
          AND EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(value->'schedule_rule'->'times')
                  AS item(value)
              WHERE item.value IS DISTINCT FROM btrim(item.value)
          )
    """)


def downgrade() -> None:
    # The previous published revision owns schedule_rule, so downgrading this
    # repair must not drop it. Whitespace cleanup is intentionally retained:
    # the discarded padding cannot be reconstructed without inventing data.
    pass
