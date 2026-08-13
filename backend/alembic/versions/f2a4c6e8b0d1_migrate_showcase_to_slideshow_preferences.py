"""migrate showcase preferences to standalone slideshow preferences

Revision ID: f2a4c6e8b0d1
Revises: f0d2e4a6b8c1
Create Date: 2026-08-11
"""

from typing import Sequence, Union

from alembic import op


revision: str = "f2a4c6e8b0d1"
down_revision: Union[str, None] = "f0d2e4a6b8c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # One statement keeps preference migration atomic. Existing slideshow
    # values win so a rolling client cannot be overwritten by legacy data.
    op.execute(
        """
        UPDATE users
        SET preferences =
          (preferences - 'showcase')
          || jsonb_build_object(
               'slideshow',
               COALESCE(
                 preferences -> 'slideshow',
                 jsonb_strip_nulls(
                   jsonb_build_object(
                     'slideDwellMs', preferences #> '{showcase,slideDwellMs}',
                     'slideTransition', preferences #> '{showcase,slideTransition}',
                     'slideLoop', preferences #> '{showcase,slideLoop}',
                     'slideShowMeta', preferences #> '{showcase,slideShowMeta}'
                   )
                 )
               )
             )
        WHERE preferences ? 'showcase'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE users
        SET preferences =
          (preferences - 'slideshow')
          || jsonb_build_object('showcase', preferences -> 'slideshow')
        WHERE preferences ? 'slideshow'
        """
    )
