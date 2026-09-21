"""align subscription source uniqueness with durable source identities

Revision ID: a6c8e0f2b4d7
Revises: 0d7e8f9a1b2c
"""

from alembic import op


revision = "a6c8e0f2b4d7"
down_revision = "0d7e8f9a1b2c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A historical schema/model drift left fresh Alembic databases enforcing
    # one row per provider, even though a provider may expose multiple durable
    # identities. Never guess which pre-existing URL should win.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM subscription_sources
            WHERE source_url IS NOT NULL
            GROUP BY subscription_id, source_url
            HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION
              'duplicate non-null subscription source URLs prevent uniqueness alignment';
          END IF;
        END
        $$
        """
    )
    op.drop_constraint(
        "uq_subscription_sources_sub_source",
        "subscription_sources",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_subscription_sources_sub_url",
        "subscription_sources",
        ["subscription_id", "source_url"],
    )


def downgrade() -> None:
    # This intentionally fails rather than deleting or merging rows when the
    # database now contains multiple identities for the same provider. Check
    # before dropping the current constraint so operators receive an actionable
    # error and the aligned schema remains unchanged.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM subscription_sources
            GROUP BY subscription_id, source
            HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION
              'duplicate subscription source providers prevent identity downgrade';
          END IF;
        END
        $$
        """
    )
    op.drop_constraint(
        "uq_subscription_sources_sub_url",
        "subscription_sources",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_subscription_sources_sub_source",
        "subscription_sources",
        ["subscription_id", "source"],
    )
