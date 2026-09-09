"""Preserve successful sidecar evidence across automatic task compaction."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision = 'fc24d6e8fa02'
down_revision = 'fb13c5d7e9a1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('storage_artifacts', sa.Column('metadata_completion_proof', postgresql.JSONB(), nullable=True))


def downgrade():
    op.drop_column('storage_artifacts', 'metadata_completion_proof')
