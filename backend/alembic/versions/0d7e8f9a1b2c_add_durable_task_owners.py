"""add durable private-history owners

Revision ID: 0d7e8f9a1b2c
Revises: f7c9e1a3b5d7
"""

from alembic import op
import sqlalchemy as sa


revision = "0d7e8f9a1b2c"
down_revision = "f7c9e1a3b5d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "download_jobs",
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "task_runs",
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
    )

    # Refuse an already-corrupt mixed provenance instead of guessing which
    # deletable trigger owns the immutable audit identity.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM download_jobs dj
            JOIN user_subscriptions us
              ON us.id = dj.triggering_user_subscription_id
            JOIN remote_accounts ra
              ON ra.id = dj.triggering_remote_account_id
            WHERE us.user_id <> ra.user_id
          ) THEN
            RAISE EXCEPTION 'download job has mixed private owners';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM task_runs tr
            JOIN user_subscriptions us
              ON us.id = tr.triggering_user_subscription_id
            JOIN remote_accounts ra
              ON ra.id = tr.triggering_remote_account_id
            WHERE us.user_id <> ra.user_id
          ) THEN
            RAISE EXCEPTION 'task run has mixed private owners';
          END IF;
        END
        $$
        """
    )
    op.execute(
        """
        UPDATE download_jobs dj
        SET owner_user_id = COALESCE(
          (SELECT us.user_id FROM user_subscriptions us
           WHERE us.id = dj.triggering_user_subscription_id),
          (SELECT ra.user_id FROM remote_accounts ra
           WHERE ra.id = dj.triggering_remote_account_id)
        )
        WHERE dj.triggering_user_subscription_id IS NOT NULL
           OR dj.triggering_remote_account_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE task_runs tr
        SET owner_user_id = COALESCE(
          (SELECT us.user_id FROM user_subscriptions us
           WHERE us.id = tr.triggering_user_subscription_id),
          (SELECT ra.user_id FROM remote_accounts ra
           WHERE ra.id = tr.triggering_remote_account_id)
        )
        WHERE tr.triggering_user_subscription_id IS NOT NULL
           OR tr.triggering_remote_account_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE task_runs tr
        SET owner_user_id = dj.owner_user_id
        FROM download_jobs dj
        WHERE tr.owner_user_id IS NULL
          AND tr.subject_type = 'download_job'
          AND tr.subject_id = dj.id
          AND dj.owner_user_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE task_runs tr
        SET owner_user_id = dj.owner_user_id
        FROM import_jobs ij
        JOIN download_jobs dj ON dj.id = ij.download_job_id
        WHERE tr.owner_user_id IS NULL
          AND tr.subject_type = 'import_job'
          AND tr.subject_id = ij.id
          AND dj.owner_user_id IS NOT NULL
        """
    )

    op.create_check_constraint(
        "ck_download_jobs_owner_user_id_positive",
        "download_jobs",
        "owner_user_id IS NULL OR owner_user_id > 0",
    )
    op.create_check_constraint(
        "ck_download_jobs_private_trigger_has_owner",
        "download_jobs",
        "(triggering_user_subscription_id IS NULL AND "
        "triggering_remote_account_id IS NULL) OR owner_user_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_task_runs_owner_user_id_positive",
        "task_runs",
        "owner_user_id IS NULL OR owner_user_id > 0",
    )
    op.create_check_constraint(
        "ck_task_runs_private_trigger_has_owner",
        "task_runs",
        "(triggering_user_subscription_id IS NULL AND "
        "triggering_remote_account_id IS NULL) OR owner_user_id IS NOT NULL",
    )
    op.create_index(
        "ix_download_jobs_owner_user_id",
        "download_jobs",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_task_runs_owner_user_id",
        "task_runs",
        ["owner_user_id"],
    )

    op.execute(
        """
        CREATE FUNCTION reject_private_history_owner_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.owner_user_id IS DISTINCT FROM NEW.owner_user_id THEN
            RAISE EXCEPTION 'private history owner is immutable';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    for table in ("download_jobs", "task_runs"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_owner_immutable
            BEFORE UPDATE OF owner_user_id ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_private_history_owner_change()
            """
        )


def downgrade() -> None:
    for table in ("task_runs", "download_jobs"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_owner_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS reject_private_history_owner_change()")
    op.drop_index("ix_task_runs_owner_user_id", table_name="task_runs")
    op.drop_index("ix_download_jobs_owner_user_id", table_name="download_jobs")
    op.drop_constraint(
        "ck_task_runs_private_trigger_has_owner",
        "task_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_task_runs_owner_user_id_positive",
        "task_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_download_jobs_private_trigger_has_owner",
        "download_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_download_jobs_owner_user_id_positive",
        "download_jobs",
        type_="check",
    )
    op.drop_column("task_runs", "owner_user_id")
    op.drop_column("download_jobs", "owner_user_id")
