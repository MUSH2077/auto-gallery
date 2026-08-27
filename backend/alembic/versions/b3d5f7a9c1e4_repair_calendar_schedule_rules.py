"""repair and normalize structured calendar schedule rules

Revision ID: b3d5f7a9c1e4
Revises: a7c9e1f3b5d7
"""

from alembic import op


revision = "b3d5f7a9c1e4"
down_revision = "a7c9e1f3b5d7"
branch_labels = None
depends_on = None

# Exact Python 3 str.isspace()/str.strip() repertoire for persisted text:
# U+0009-U+000D, U+001C-U+001F, U+0020, U+0085, U+00A0, U+1680,
# U+2000-U+200A, U+2028-U+2029, U+202F, U+205F, and U+3000.
_PYTHON_STRIP_CHARACTERS_SQL = (
    r"U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F"
    r"\0020\0085\00A0\1680\2000\2001\2002\2003\2004\2005"
    r"\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000'"
)
_TRIMMED_SCHEDULE_TIME_SQL = (
    f"btrim(item.value, {_PYTHON_STRIP_CHARACTERS_SQL})"
)


def upgrade() -> None:
    # Some databases were stamped at the previous published head without the
    # column physically present. Alembic will not replay a stamped revision, so
    # repair that drift in a new revision rather than editing migration history.
    op.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS schedule_rule JSONB")
    op.execute(f"""
        UPDATE subscriptions
        SET schedule_rule = jsonb_set(
            schedule_rule,
            '{{times}}',
            COALESCE((
                SELECT jsonb_agg(
                    {_TRIMMED_SCHEDULE_TIME_SQL}
                    ORDER BY item.ordinality
                )
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
              WHERE item.value IS DISTINCT FROM {_TRIMMED_SCHEDULE_TIME_SQL}
          )
    """)
    op.execute(f"""
        UPDATE system_settings
        SET value = jsonb_set(
            value,
            '{{schedule_rule,times}}',
            COALESCE((
                SELECT jsonb_agg(
                    {_TRIMMED_SCHEDULE_TIME_SQL}
                    ORDER BY item.ordinality
                )
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
              WHERE item.value IS DISTINCT FROM {_TRIMMED_SCHEDULE_TIME_SQL}
          )
    """)


def downgrade() -> None:
    # The previous published revision owns schedule_rule, so downgrading this
    # repair must not drop it. Whitespace cleanup is intentionally retained:
    # the discarded padding cannot be reconstructed without inventing data.
    pass
