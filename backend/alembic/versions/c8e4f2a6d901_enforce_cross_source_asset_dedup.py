"""enforce cross-source asset dedup scope

Revision ID: c8e4f2a6d901
Revises: b7d3e9f1a2c4
Create Date: 2026-07-27
"""

from typing import Sequence, Union

from alembic import op


revision: str = "c8e4f2a6d901"
down_revision: Union[str, None] = "b7d3e9f1a2c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INVALID_PAIR = """
    NOT EXISTS (
        SELECT 1
        FROM asset_sources axs
        JOIN work_sources ws ON ws.id = axs.work_source_id
        WHERE axs.asset_id = c.left_asset_id
    )
    OR NOT EXISTS (
        SELECT 1
        FROM asset_sources axs
        JOIN work_sources ws ON ws.id = axs.work_source_id
        WHERE axs.asset_id = c.right_asset_id
    )
    OR EXISTS (
        SELECT 1
        FROM asset_sources left_axs
        JOIN work_sources left_ws ON left_ws.id = left_axs.work_source_id
        JOIN asset_sources right_axs
          ON right_axs.asset_id = c.right_asset_id
        JOIN work_sources right_ws ON right_ws.id = right_axs.work_source_id
        WHERE left_axs.asset_id = c.left_asset_id
          AND left_ws.work_id = right_ws.work_id
    )
    OR EXISTS (
        SELECT 1
        FROM asset_sources left_axs
        JOIN work_sources left_ws ON left_ws.id = left_axs.work_source_id
        JOIN asset_sources right_axs
          ON right_axs.asset_id = c.right_asset_id
        JOIN work_sources right_ws ON right_ws.id = right_axs.work_source_id
        WHERE left_axs.asset_id = c.left_asset_id
          AND (
            CASE lower(left_ws.source)
              WHEN 'twitter' THEN 'x'
              ELSE lower(left_ws.source)
            END
          ) = (
            CASE lower(right_ws.source)
              WHEN 'twitter' THEN 'x'
              ELSE lower(right_ws.source)
            END
          )
    )
"""


def upgrade() -> None:
    # Storage side effects may already exist for a merged case/group. Never
    # erase such history silently; require explicit recovery if an installation
    # has legacy invalid groups.
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM asset_dedup_cases c
            WHERE c.status = 'merged'
              AND ({_INVALID_PAIR})
          ) THEN
            RAISE EXCEPTION
              'cross-source dedup migration found an invalid merged case';
          END IF;

          IF EXISTS (
            SELECT 1
            FROM visual_asset_groups g
            WHERE
              (SELECT count(*)
               FROM visual_asset_members vm
               WHERE vm.group_id = g.id) < 2
              OR EXISTS (
                SELECT 1
                FROM visual_asset_members vm
                WHERE vm.group_id = g.id
                  AND NOT EXISTS (
                    SELECT 1
                    FROM asset_sources axs
                    JOIN work_sources ws
                      ON ws.id = axs.work_source_id
                    WHERE axs.asset_id = vm.asset_id
                  )
              )
              OR EXISTS (
                SELECT 1
                FROM visual_asset_members left_vm
                JOIN visual_asset_members right_vm
                  ON right_vm.group_id = left_vm.group_id
                 AND right_vm.asset_id > left_vm.asset_id
                JOIN asset_sources left_axs
                  ON left_axs.asset_id = left_vm.asset_id
                JOIN work_sources left_ws
                  ON left_ws.id = left_axs.work_source_id
                JOIN asset_sources right_axs
                  ON right_axs.asset_id = right_vm.asset_id
                JOIN work_sources right_ws
                  ON right_ws.id = right_axs.work_source_id
                WHERE left_vm.group_id = g.id
                  AND (
                    left_ws.work_id = right_ws.work_id
                    OR (
                      CASE lower(left_ws.source)
                        WHEN 'twitter' THEN 'x'
                        ELSE lower(left_ws.source)
                      END
                    ) = (
                      CASE lower(right_ws.source)
                        WHEN 'twitter' THEN 'x'
                        ELSE lower(right_ws.source)
                      END
                    )
                  )
              )
          ) THEN
            RAISE EXCEPTION
              'cross-source dedup migration found an invalid visual group';
          END IF;
        END
        $$;
        """
    )

    op.execute(
        f"""
        WITH invalid_cases AS (
          SELECT c.id
          FROM asset_dedup_cases c
          WHERE c.status <> 'merged'
            AND ({_INVALID_PAIR})
        )
        DELETE FROM asset_dedup_decisions d
        USING invalid_cases invalid
        WHERE d.case_id = invalid.id
        """
    )
    op.execute(
        f"""
        DELETE FROM asset_dedup_cases c
        WHERE c.status <> 'merged'
          AND ({_INVALID_PAIR})
        """
    )
    op.execute(
        """
        DELETE FROM asset_dedup_evidence evidence
        WHERE NOT EXISTS (
            SELECT 1
            FROM asset_dedup_cases cases
            WHERE cases.evidence_id = evidence.id
        )
          AND NOT EXISTS (
            SELECT 1
            FROM visual_asset_members members
            WHERE members.evidence_id = evidence.id
        )
        """
    )


def downgrade() -> None:
    # Scope-excluded cases are derived data and can be regenerated only by the
    # legacy algorithm. Downgrade intentionally leaves the cleaned data intact.
    pass
