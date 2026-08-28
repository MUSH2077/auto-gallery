"""Immutable user ownership for private download and task history."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import pytest
from sqlalchemy import select, text


OWNER_REVISION = "0d7e8f9a1b2c"


def test_download_and_task_owners_are_durable_indexed_audit_identifiers():
    """Owner ids survive user deletion and can never become invalid sentinels."""

    import app.models  # noqa: F401 - register metadata
    from app.models import Base

    jobs = Base.metadata.tables["download_jobs"]
    tasks = Base.metadata.tables["task_runs"]
    for table in (jobs, tasks):
        owner = table.c.owner_user_id
        assert owner.nullable is True
        assert not owner.foreign_keys
        assert any(
            tuple(column.name for column in index.columns) == ("owner_user_id",)
            for index in table.indexes
        )
        assert any(
            constraint.name == f"ck_{table.name}_owner_user_id_positive"
            for constraint in table.constraints
        )


def test_owner_migration_is_the_only_head_and_follows_credential_generation():
    """The durable-owner schema change stays additive on the reviewed head."""

    backend_dir = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{OWNER_REVISION} (head)"


@pytest.mark.asyncio
async def test_private_task_mutations_fail_closed_without_authenticated_user(
    monkeypatch,
):
    """Internal/future callers cannot bypass a durable owner with user=None."""

    from fastapi import HTTPException

    from app.api import tasks as tasks_api

    task = type("PrivateTask", (), {"owner_user_id": 42})()

    class StubTaskService:
        def __init__(self, _db):
            pass

        async def get(self, _task_id):
            return task

        async def is_visible_to_user(self, *_args):
            raise AssertionError("missing users must be rejected before visibility lookup")

    monkeypatch.setattr(tasks_api, "TaskService", StubTaskService)
    for operation in (
        tasks_api.acknowledge_task(
            uuid4(),
            db=object(),
            operator="audit-test",
            user=None,
        ),
        tasks_api._control_task(
            uuid4(),
            "cancel",
            object(),
            "audit-test",
            user=None,
        ),
    ):
        with pytest.raises(HTTPException) as denied:
            await operation
        assert denied.value.status_code == 404


@pytest.mark.integration
def test_owner_migration_backfills_private_provenance_and_is_idempotent(
    test_database_url,
    test_db_base_url,
):
    """Upgrade derives immutable owners while preserving genuinely global rows."""

    import asyncpg

    database_name = f"autogallery_durable_owner_{uuid4().hex[:10]}"
    database_url = urlunparse(
        urlparse(test_database_url)._replace(path=f"/{database_name}")
    )
    asyncpg_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    backend_dir = Path(__file__).resolve().parents[1]

    async def create_database() -> None:
        admin = await asyncpg.connect(test_db_base_url, timeout=3)
        try:
            await admin.execute(f'CREATE DATABASE "{database_name}"')
        finally:
            await admin.close()

    async def seed_predecessor() -> None:
        connection = await asyncpg.connect(asyncpg_url, timeout=3)
        try:
            owner = await connection.fetchval(
                "INSERT INTO users (username, password_hash) "
                "VALUES ('durable-owner-migration', 'test-only') RETURNING id"
            )
            await connection.execute(
                "INSERT INTO creators (id, name) VALUES "
                "('20000000-0000-0000-0000-000000000001', 'durable-owner-creator')"
            )
            await connection.execute(
                "INSERT INTO subscriptions (id, creator_id, name) VALUES ("
                "'20000000-0000-0000-0000-000000000002', "
                "'20000000-0000-0000-0000-000000000001', 'durable-owner-sub')"
            )
            await connection.execute(
                "INSERT INTO subscription_sources ("
                "id, subscription_id, source, source_creator_id, source_url"
                ") VALUES ('20000000-0000-0000-0000-000000000012', "
                "'20000000-0000-0000-0000-000000000002', 'pixiv', '1', "
                "'https://www.pixiv.net/users/1')"
            )
            await connection.execute(
                "INSERT INTO user_subscriptions (id, user_id, subscription_id, name) "
                "VALUES ('20000000-0000-0000-0000-000000000003', $1, "
                "'20000000-0000-0000-0000-000000000002', 'private')",
                owner,
            )
            await connection.execute(
                "INSERT INTO remote_accounts (id, user_id, source) VALUES ("
                "'20000000-0000-0000-0000-000000000004', $1, 'pixiv')",
                owner,
            )
            await connection.execute(
                "INSERT INTO download_jobs ("
                "id, subscription_id, subscription_source_id, source, source_url, status, "
                "triggering_user_subscription_id) VALUES ("
                "'20000000-0000-0000-0000-000000000005', "
                "'20000000-0000-0000-0000-000000000002', "
                "'20000000-0000-0000-0000-000000000012', "
                "'pixiv', "
                "'https://www.pixiv.net/users/1', 'complete', "
                "'20000000-0000-0000-0000-000000000003'), ("
                "'20000000-0000-0000-0000-000000000006', "
                "'20000000-0000-0000-0000-000000000002', "
                "'20000000-0000-0000-0000-000000000012', 'pixiv', "
                "'https://www.pixiv.net/users/2', 'complete', NULL)"
            )
            await connection.execute(
                "INSERT INTO import_jobs (id, download_job_id, status) VALUES ("
                "'20000000-0000-0000-0000-000000000007', "
                "'20000000-0000-0000-0000-000000000005', 'complete')"
            )
            await connection.execute(
                "INSERT INTO task_runs ("
                "id, kind, subject_type, subject_id, status, resource_state, "
                "triggering_remote_account_id) VALUES ("
                "'20000000-0000-0000-0000-000000000008', 'discovery', NULL, NULL, "
                "'complete', 'waiting', '20000000-0000-0000-0000-000000000004'), ("
                "'20000000-0000-0000-0000-000000000009', 'download', 'download_job', "
                "'20000000-0000-0000-0000-000000000005', 'complete', 'waiting', NULL), ("
                "'20000000-0000-0000-0000-000000000010', 'import', 'import_job', "
                "'20000000-0000-0000-0000-000000000007', 'complete', 'waiting', NULL), ("
                "'20000000-0000-0000-0000-000000000011', 'admin', NULL, NULL, "
                "'complete', 'waiting', NULL)"
            )
        finally:
            await connection.close()

    async def inspect() -> tuple[list[int | None], list[int | None], set[str]]:
        connection = await asyncpg.connect(asyncpg_url, timeout=3)
        try:
            jobs = [
                row["owner_user_id"]
                for row in await connection.fetch(
                    "SELECT owner_user_id FROM download_jobs "
                    "WHERE id::text LIKE '20000000-%' ORDER BY id"
                )
            ]
            tasks = [
                row["owner_user_id"]
                for row in await connection.fetch(
                    "SELECT owner_user_id FROM task_runs "
                    "WHERE id::text LIKE '20000000-%' ORDER BY id"
                )
            ]
            indexes = {
                row["indexname"]
                for row in await connection.fetch(
                    "SELECT indexname FROM pg_indexes WHERE indexname IN ("
                    "'ix_download_jobs_owner_user_id', 'ix_task_runs_owner_user_id')"
                )
            }
            return jobs, tasks, indexes
        finally:
            await connection.close()

    async def assert_immutable() -> None:
        connection = await asyncpg.connect(asyncpg_url, timeout=3)
        try:
            with pytest.raises(
                asyncpg.RaiseError,
                match="private history owner is immutable",
            ):
                async with connection.transaction():
                    await connection.execute(
                        "UPDATE download_jobs SET owner_user_id = owner_user_id + 1 "
                        "WHERE id = '20000000-0000-0000-0000-000000000005'"
                    )
        finally:
            await connection.close()

    async def drop_database() -> None:
        admin = await asyncpg.connect(test_db_base_url, timeout=3)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        finally:
            await admin.close()

    environment = {
        **os.environ,
        "DATABASE_URL": database_url,
        "APP_CONFIG_ROOT": "/tmp/auto-gallery-durable-owner-config",
        "SECRET_KEY": "durable-owner-test-secret",
        "ADMIN_PASSWORD": "durable-owner-test-password",
        "REDIS_URL": "redis://:dummy@localhost:6379/0",
    }

    def alembic(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=backend_dir,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )

    try:
        asyncio.run(create_database())
        predecessor = alembic("upgrade", "f7c9e1a3b5d7")
        assert predecessor.returncode == 0, predecessor.stderr
        asyncio.run(seed_predecessor())
        upgraded = alembic("upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr
        repeated = alembic("upgrade", "head")
        assert repeated.returncode == 0, repeated.stderr
        jobs, tasks, indexes = asyncio.run(inspect())
        assert jobs[0] is not None
        assert jobs[1] is None
        assert tasks[:3] == [jobs[0], jobs[0], jobs[0]]
        assert tasks[3] is None
        assert indexes == {
            "ix_download_jobs_owner_user_id",
            "ix_task_runs_owner_user_id",
        }
        asyncio.run(assert_immutable())
    finally:
        asyncio.run(drop_database())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_membership_removal_never_transfers_private_job_import_task_or_events():
    """Deleting A's membership must not expose A's retained history to member B."""

    from app.database import async_session, engine
    from app.models import (
        Creator,
        DownloadJob,
        ImportJob,
        Subscription,
        SubscriptionSource,
        TaskEvent,
        TaskRun,
        User,
        UserSubscription,
        UserSubscriptionSource,
    )
    from app.services.subscription_membership import SubscriptionMembershipService
    from app.services.tasks import (
        download_job_visibility_condition,
        import_job_visibility_condition,
        task_visibility_condition,
    )

    marker = f"durable_owner_{uuid4().hex}"
    task_ids = []
    private_job_id = None
    legacy_job_id = None
    try:
        async with async_session() as db:
            users = [
                User(
                    username=f"{marker}_{suffix}",
                    password_hash="test-only",
                    is_active=True,
                    permissions=["subscriptions", "tasks"],
                )
                for suffix in ("a", "b")
            ]
            creator = Creator(name=marker)
            db.add_all([*users, creator])
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name=marker)
            db.add(subscription)
            await db.flush()
            source = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id=marker,
                source_url="https://www.pixiv.net/users/7654321",
            )
            members = [
                UserSubscription(
                    user_id=user.id,
                    subscription_id=subscription.id,
                    name=user.username,
                )
                for user in users
            ]
            db.add_all([source, *members])
            await db.flush()
            db.add_all(
                [
                    UserSubscriptionSource(
                        user_id=user.id,
                        subscription_id=subscription.id,
                        user_subscription_id=member.id,
                        subscription_source_id=source.id,
                        remote_account_id=None,
                        is_enabled=True,
                        auth_healthy=True,
                        auth_status="healthy",
                    )
                    for user, member in zip(users, members, strict=True)
                ]
            )
            private_job = DownloadJob(
                subscription_id=subscription.id,
                subscription_source_id=source.id,
                triggering_user_subscription_id=members[0].id,
                source="pixiv",
                source_url=source.source_url,
                status="complete",
            )
            # Before the migration this is an unmapped attribute. That lets the
            # RED exercise the existing deletion leak instead of failing at the
            # constructor boundary; after migration it is durable state.
            private_job.owner_user_id = users[0].id
            legacy_job = DownloadJob(
                subscription_id=subscription.id,
                subscription_source_id=source.id,
                source="pixiv",
                source_url=source.source_url,
                status="complete",
            )
            db.add_all([private_job, legacy_job])
            await db.flush()
            import_job = ImportJob(download_job_id=private_job.id, status="complete")
            db.add(import_job)
            await db.flush()
            from app.services.tasks import TaskService

            task_service = TaskService(db)
            private_task = await task_service.ensure_download_task(private_job)
            private_import_task = await task_service.ensure_import_task(import_job)
            assert private_task.owner_user_id == users[0].id
            assert private_import_task.owner_user_id == users[0].id
            with pytest.raises(ValueError, match="mixed owners"):
                await task_service.create_task(
                    kind="download",
                    operation_type="download",
                    title="must not transfer",
                    subject_type="download_job",
                    subject_id=private_job.id,
                    owner_user_id=users[1].id,
                )
            event = TaskEvent(
                task_run_id=private_task.id,
                event_type="transition",
                to_status="complete",
                message="private history",
            )
            db.add(event)
            await db.commit()
            task_ids = [private_task.id, private_import_task.id]
            private_job_id = private_job.id
            legacy_job_id = legacy_job.id
            user_ids = [user.id for user in users]
            subscription_id = subscription.id
            import_job_id = import_job.id
            event_id = event.id

            async def visible_job(user_id: int, job_id):
                return (
                    await db.execute(
                        select(DownloadJob.id).where(
                            DownloadJob.id == job_id,
                            download_job_visibility_condition(user_id),
                        )
                    )
                ).scalar_one_or_none()

            async def visible_import(user_id: int):
                return (
                    await db.execute(
                        select(ImportJob.id).where(
                            ImportJob.id == import_job_id,
                            import_job_visibility_condition(user_id),
                        )
                    )
                ).scalar_one_or_none()

            async def visible_task(user_id: int):
                return (
                    await db.execute(
                        select(TaskRun.id).where(
                            TaskRun.id == task_ids[0],
                            task_visibility_condition(user_id),
                        )
                    )
                ).scalar_one_or_none()

            assert await visible_job(user_ids[0], private_job_id) == private_job_id
            assert await visible_job(user_ids[1], private_job_id) is None
            assert await visible_import(user_ids[1]) is None
            assert await visible_task(user_ids[1]) is None
            assert not await task_service.is_visible_to_user(
                private_import_task,
                user_ids[1],
            )

            await SubscriptionMembershipService(db, user_ids[0]).remove(
                subscription_id
            )
            await db.commit()
            db.expire_all()

            # This is the original branch bug: SET NULL erases the only owner
            # evidence and the shared-subscription peer starts matching it.
            assert await visible_job(user_ids[1], private_job_id) is None
            assert await visible_import(user_ids[1]) is None
            assert await visible_task(user_ids[1]) is None

            assert await visible_job(user_ids[0], private_job_id) == private_job_id
            assert await visible_import(user_ids[0]) == import_job_id
            assert await visible_task(user_ids[0]) == task_ids[0]
            stored_job = await db.get(DownloadJob, private_job_id)
            stored_task = await db.get(TaskRun, task_ids[0])
            assert stored_job.owner_user_id == user_ids[0]
            assert stored_task.owner_user_id == user_ids[0]
            assert (
                await db.execute(
                    select(TaskEvent.id)
                    .join(TaskRun, TaskRun.id == TaskEvent.task_run_id)
                    .where(
                        TaskEvent.id == event_id,
                        task_visibility_condition(user_ids[0]),
                    )
                )
            ).scalar_one() == event_id

            # User deletion must neither fail on retained audit ownership nor
            # transform that history into a legacy/shared row.
            from app.services.users import UserService

            await UserService(db).delete(user_ids[0])
            assert await db.get(User, user_ids[0]) is None
            assert await visible_job(user_ids[1], private_job_id) is None
            assert await visible_import(user_ids[1]) is None
            assert await visible_task(user_ids[1]) is None
            assert await visible_job(user_ids[0], private_job_id) == private_job_id
            assert await visible_import(user_ids[0]) == import_job_id
            assert await visible_task(user_ids[0]) == task_ids[0]

            from app.services.search import SearchService

            peer_jobs = await SearchService(db).search_download_jobs(
                "",
                user_id=user_ids[1],
            )
            assert private_job_id not in {job.id for job in peer_jobs}
            assert legacy_job_id in {job.id for job in peer_jobs}
            peer_import_total, peer_imports = await SearchService(
                db
            ).search_import_jobs("", user_id=user_ids[1])
            assert peer_import_total == len(peer_imports)
            assert import_job_id not in {job.id for job in peer_imports}

            # Truly legacy/global rows retain shared membership inference.
            assert await visible_job(user_ids[1], legacy_job_id) == legacy_job_id
    finally:
        async with async_session() as db:
            params = {"marker": f"{marker}%"}
            if task_ids:
                await db.execute(
                    text("DELETE FROM task_events WHERE task_run_id = ANY(:task_ids)"),
                    {"task_ids": task_ids},
                )
                await db.execute(
                    text("DELETE FROM task_runs WHERE id = ANY(:task_ids)"),
                    {"task_ids": task_ids},
                )
            await db.execute(
                text(
                    "DELETE FROM import_jobs WHERE download_job_id IN ("
                    "SELECT id FROM download_jobs WHERE subscription_id IN ("
                    "SELECT s.id FROM subscriptions s JOIN creators c ON c.id=s.creator_id "
                    "WHERE c.name LIKE :marker))"
                ),
                params,
            )
            job_ids = [value for value in (private_job_id, legacy_job_id) if value]
            if job_ids:
                await db.execute(
                    text("DELETE FROM download_jobs WHERE id = ANY(:job_ids)"),
                    {"job_ids": job_ids},
                )
            for table in ("user_subscription_sources", "user_subscriptions"):
                await db.execute(
                    text(
                        f"DELETE FROM {table} WHERE user_id IN ("
                        "SELECT id FROM users WHERE username LIKE :marker)"
                    ),
                    params,
                )
            await db.execute(
                text(
                    "DELETE FROM subscription_sources WHERE subscription_id IN ("
                    "SELECT s.id FROM subscriptions s JOIN creators c ON c.id=s.creator_id "
                    "WHERE c.name LIKE :marker)"
                ),
                params,
            )
            await db.execute(
                text(
                    "DELETE FROM subscriptions WHERE creator_id IN ("
                    "SELECT id FROM creators WHERE name LIKE :marker)"
                ),
                params,
            )
            await db.execute(text("DELETE FROM creators WHERE name LIKE :marker"), params)
            await db.execute(text("DELETE FROM users WHERE username LIKE :marker"), params)
            await db.commit()
        await engine.dispose()
