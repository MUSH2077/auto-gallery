"""Add candidate evidence lifecycle and separate download authentication health.

Revision ID: d0f2a4c6e8b1
Revises: c9e1a3b5d7f2
"""

from alembic import op
import sqlalchemy as sa


revision = "d0f2a4c6e8b1"
down_revision = "c9e1a3b5d7f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "discovery_candidates",
        sa.Column("evidence_status", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "discovery_candidates",
        sa.Column("evidence_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "discovery_candidates",
        sa.Column("evidence_error_code", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "discovery_candidates",
        sa.Column("evidence_version", sa.Integer(), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE discovery_candidates AS candidate
            SET evidence_status = CASE
                    WHEN account.source = 'pixiv' THEN 'ready'
                    WHEN candidate.state IN ('imported', 'dismissed') THEN 'not_required'
                    ELSE 'pending'
                END,
                evidence_version = 1
            FROM remote_accounts AS account
            WHERE account.id = candidate.remote_account_id
            """
        )
    )
    op.alter_column(
        "discovery_candidates",
        "evidence_status",
        nullable=False,
        server_default=sa.text("'pending'"),
    )
    op.alter_column(
        "discovery_candidates",
        "evidence_version",
        nullable=False,
        server_default=sa.text("1"),
    )
    op.create_check_constraint(
        "ck_discovery_candidates_evidence_status",
        "discovery_candidates",
        "evidence_status IN ('pending', 'ready', 'retrying', 'failed', 'not_required')",
    )
    op.create_index(
        "ix_discovery_candidates_account_evidence",
        "discovery_candidates",
        ["remote_account_id", "evidence_status", "id"],
    )

    op.add_column(
        "remote_accounts",
        sa.Column("download_auth_status", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "remote_accounts",
        sa.Column("download_auth_error_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "remote_accounts",
        sa.Column("last_download_auth_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE remote_accounts
            SET download_auth_status = CASE
                WHEN source = 'x' AND auth_method = 'oauth2' AND
                     COALESCE(credential_metadata->'fields', '[]'::jsonb) ? 'download_cookie'
                    THEN 'personal'
                WHEN source = 'x' AND auth_method = 'oauth2'
                    THEN 'anonymous_only'
                WHEN credential_ciphertext IS NOT NULL
                    THEN 'personal'
                ELSE 'unavailable'
            END
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE remote_accounts
            SET auth_status = 'unhealthy',
                auth_error_reason = 'missing_tweet_read_scope'
            WHERE source = 'x'
              AND auth_method = 'oauth2'
              AND NOT (COALESCE(scopes, '[]'::jsonb) @> '["tweet.read"]'::jsonb)
              AND auth_status IS DISTINCT FROM 'deleted'
            """
        )
    )
    op.alter_column(
        "remote_accounts",
        "download_auth_status",
        nullable=False,
        server_default=sa.text("'unavailable'"),
    )
    op.create_check_constraint(
        "ck_remote_accounts_download_auth_status",
        "remote_accounts",
        "download_auth_status IN ('personal', 'anonymous_only', 'unhealthy', 'unavailable')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_remote_accounts_download_auth_status",
        "remote_accounts",
        type_="check",
    )
    op.drop_column("remote_accounts", "last_download_auth_checked_at")
    op.drop_column("remote_accounts", "download_auth_error_reason")
    op.drop_column("remote_accounts", "download_auth_status")
    op.drop_index(
        "ix_discovery_candidates_account_evidence",
        table_name="discovery_candidates",
    )
    op.drop_constraint(
        "ck_discovery_candidates_evidence_status",
        "discovery_candidates",
        type_="check",
    )
    op.drop_column("discovery_candidates", "evidence_version")
    op.drop_column("discovery_candidates", "evidence_error_code")
    op.drop_column("discovery_candidates", "evidence_checked_at")
    op.drop_column("discovery_candidates", "evidence_status")
