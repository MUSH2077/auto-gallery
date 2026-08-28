"""Shared source scheduling and private remote-account authentication."""

from __future__ import annotations

from contextlib import asynccontextmanager
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest


async def _cleanup_shared_test_rows(db) -> None:
    from sqlalchemy import text

    params = {"prefix": "shared_schedule_test_%"}
    shared_jobs = (
        "SELECT dj.id FROM download_jobs dj JOIN subscriptions s ON s.id=dj.subscription_id "
        "JOIN creators c ON c.id=s.creator_id WHERE c.name LIKE :prefix"
    )
    await db.execute(
        text(
            f"DELETE FROM task_events WHERE task_run_id IN (SELECT id FROM task_runs WHERE "
            f"subject_type='download_job' AND subject_id IN ({shared_jobs}))"
        ),
        params,
    )
    await db.execute(
        text(
            f"DELETE FROM task_runs WHERE subject_type='download_job' AND subject_id IN ({shared_jobs})"
        ),
        params,
    )
    await db.execute(
        text(
            "DELETE FROM task_events WHERE task_run_id IN (SELECT tr.id FROM task_runs tr "
            "LEFT JOIN remote_accounts ra ON ra.id=tr.triggering_remote_account_id "
            "LEFT JOIN user_subscriptions us ON us.id=tr.triggering_user_subscription_id "
            "WHERE ra.user_id IN (SELECT id FROM users WHERE username LIKE :prefix) "
            "OR us.user_id IN (SELECT id FROM users WHERE username LIKE :prefix))"
        ),
        params,
    )
    await db.execute(
        text(
            "DELETE FROM task_runs WHERE triggering_remote_account_id IN (SELECT id FROM "
            "remote_accounts WHERE user_id IN (SELECT id FROM users WHERE username LIKE :prefix)) "
            "OR triggering_user_subscription_id IN (SELECT id FROM user_subscriptions WHERE "
            "user_id IN (SELECT id FROM users WHERE username LIKE :prefix))"
        ),
        params,
    )
    await db.execute(
        text(
            f"DELETE FROM import_jobs WHERE download_job_id IN ({shared_jobs})"
        ),
        params,
    )
    await db.execute(
        text(
            f"DELETE FROM download_jobs WHERE id IN ({shared_jobs})"
        ),
        params,
    )
    for table in (
        "discovery_candidates",
        "user_subscription_sources",
        "remote_accounts",
        "user_subscriptions",
    ):
        await db.execute(
            text(
                f"DELETE FROM {table} WHERE user_id IN "
                "(SELECT id FROM users WHERE username LIKE :prefix)"
            ),
            params,
        )
    await db.execute(
        text(
            "DELETE FROM subscription_sources WHERE subscription_id IN (SELECT s.id FROM "
            "subscriptions s JOIN creators c ON c.id=s.creator_id WHERE c.name LIKE :prefix)"
        ),
        params,
    )
    await db.execute(
        text(
            "DELETE FROM subscriptions WHERE creator_id IN (SELECT id FROM creators WHERE name LIKE :prefix)"
        ),
        params,
    )
    await db.execute(text("DELETE FROM creators WHERE name LIKE :prefix"), params)
    await db.execute(text("DELETE FROM users WHERE username LIKE :prefix"), params)
    await db.commit()


async def _seed_shared_source(db, *, now: datetime):
    from app.models import (
        Creator,
        RemoteAccount,
        Subscription,
        SubscriptionSource,
        User,
        UserSubscription,
        UserSubscriptionSource,
    )

    await _cleanup_shared_test_rows(db)
    users = [
        User(
            username=f"shared_schedule_test_{label}_{uuid4().hex[:8]}",
            password_hash="test-only",
            is_active=True,
        )
        for label in ("unhealthy", "healthy")
    ]
    db.add_all(users)
    creator = Creator(name=f"shared_schedule_test_{uuid4().hex}")
    db.add(creator)
    await db.flush()
    subscription = Subscription(
        creator_id=creator.id,
        is_active=True,
        sync_enabled=True,
        sync_interval_hours=6,
        schedule_mode="interval",
    )
    db.add(subscription)
    await db.flush()
    source = SubscriptionSource(
        subscription_id=subscription.id,
        source="pixiv",
        source_url="https://www.pixiv.net/users/42001",
        is_enabled=True,
    )
    db.add(source)
    await db.flush()
    members = [
        UserSubscription(
            user_id=user.id,
            subscription_id=subscription.id,
            is_active=True,
            sync_enabled=True,
            sync_interval_hours=hours,
            schedule_mode="interval",
        )
        for user, hours in zip(users, (2, 8), strict=True)
    ]
    accounts = [
        RemoteAccount(
            user_id=user.id,
            source="pixiv",
            auth_method="refresh_token",
            credential_ciphertext=f"test-ciphertext-{index}",
            credential_key_version=1,
            is_enabled=True,
            auth_status=status,
        )
        for index, (user, status) in enumerate(
            zip(users, ("unhealthy", "healthy"), strict=True)
        )
    ]
    db.add_all([*members, *accounts])
    await db.flush()
    dues = (now - timedelta(hours=2), now - timedelta(hours=1))
    bindings = [
        UserSubscriptionSource(
            user_id=user.id,
            subscription_id=subscription.id,
            user_subscription_id=member.id,
            subscription_source_id=source.id,
            remote_account_id=account.id,
            is_enabled=True,
            auth_healthy=True,
            next_sync_at=due,
        )
        for user, member, account, due in zip(
            users, members, accounts, dues, strict=True
        )
    ]
    db.add_all(bindings)
    await db.flush()
    return users, creator, subscription, source, members, accounts, bindings, dues


@pytest.mark.integration
@pytest.mark.asyncio
async def test_canonical_source_cache_uses_earliest_usable_member_due_time():
    """An unhealthy early account must not disable or advance a healthy peer."""

    from app.database import async_session, engine
    from app.models import (
        Creator,
        RemoteAccount,
        Subscription,
        SubscriptionSource,
        User,
        UserSubscription,
        UserSubscriptionSource,
    )
    from app.services.subscription_membership import recompute_subscription_membership_cache

    now = datetime.now(timezone.utc)
    try:
        async with async_session() as db:
            (
                _users,
                _creator,
                subscription,
                source,
                _members,
                _accounts,
                _bindings,
                (_early_due, healthy_due),
            ) = await _seed_shared_source(db, now=now)

            await recompute_subscription_membership_cache(db, subscription.id)

            assert source.is_enabled is True
            assert source.auth_healthy is True
            assert source.next_sync_at == healthy_due
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_null_account_binding_remains_eligible_for_global_config():
    """Backfilled bindings without an account keep the legacy download path."""

    from app.database import async_session, engine
    from app.services.subscription_membership import (
        recompute_subscription_membership_cache,
        select_eligible_membership_source,
    )

    now = datetime.now(timezone.utc)
    try:
        async with async_session() as db:
            (
                _users,
                _creator,
                subscription,
                source,
                members,
                _accounts,
                bindings,
                dues,
            ) = await _seed_shared_source(db, now=now)
            bindings[0].remote_account_id = None
            await db.flush()

            await recompute_subscription_membership_cache(db, subscription.id)
            selected = await select_eligible_membership_source(db, source, now=now)

            assert selected is not None
            assert selected.membership.id == members[0].id
            assert selected.account is None
            assert source.next_sync_at == dues[0]
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_member_selection_has_no_global_fallback_when_every_credential_is_unhealthy():
    """Personal bindings never silently fall through to another/global credential."""

    from app.database import async_session, engine
    from app.services.subscription_membership import (
        recompute_subscription_membership_cache,
        select_eligible_membership_source,
    )

    now = datetime.now(timezone.utc)
    try:
        async with async_session() as db:
            (
                _users,
                _creator,
                subscription,
                source,
                _members,
                accounts,
                _bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            accounts[1].auth_status = "unhealthy"
            await recompute_subscription_membership_cache(db, subscription.id)

            selected = await select_eligible_membership_source(db, source, now=now)

            assert selected is None
            assert source.is_enabled is False
            assert source.auth_healthy is False
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_personal_download_auth_uses_0600_ephemeral_config_and_leaves_no_durable_secret(
    tmp_path,
):
    """Credential plaintext exists only in the worker-owned 0600 temp overlay."""

    from app.database import async_session, engine
    from app.jobs import download as download_job
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter
    from app.remote_discovery.registry import DiscoveryAdapterRegistry
    from app.services.remote_credentials import CredentialVault

    materialize = getattr(download_job, "_materialize_personal_download_config", None)
    assert materialize is not None, "download worker must materialize private auth at execution"

    canary = "task4-secret-canary-refresh-token"
    vault = CredentialVault(base64.urlsafe_b64encode(b"q" * 32).decode())
    adapters = DiscoveryAdapterRegistry()
    adapters.register(PixivRemoteDiscoveryAdapter())
    now = datetime.now(timezone.utc)
    try:
        async with async_session() as db:
            (
                _users,
                _creator,
                subscription,
                source,
                members,
                accounts,
                _bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            account = accounts[1]
            account.credential_ciphertext = vault.encrypt(
                {"refresh_token": canary},
                user_id=account.user_id,
                source=account.source,
                account_id=account.id,
            )
            job = SimpleNamespace(
                id=uuid4(),
                subscription_id=subscription.id,
                subscription_source_id=source.id,
                triggering_user_subscription_id=members[1].id,
                triggering_remote_account_id=account.id,
                source="pixiv",
                gallerydl_config_path=None,
                manifest={"safe": True},
            )
            await db.flush()

            personal = await materialize(
                db,
                job,
                {"extractor": {"pixiv": {"filename": "{id}.{extension}"}}},
                config_root=tmp_path,
                vault=vault,
                adapters=adapters,
            )
            assert personal is not None
            config_path = personal.path
            assert os.stat(config_path).st_mode & 0o777 == 0o600
            config = json.loads(config_path.read_text())
            assert config["extractor"]["pixiv"] == {
                "filename": "{id}.{extension}",
                "refresh-token": canary,
            }
            assert personal.redact(f"prefix {canary} suffix") == "prefix ***REDACTED*** suffix"
            durable_command = download_job._durable_gallerydl_command(
                ["gallery-dl", "--config", str(config_path), job.source],
                personal,
            )
            assert durable_command == ["gallery-dl", job.source]
            assert job.gallerydl_config_path is None
            assert canary not in json.dumps(job.manifest)

            download_job._cleanup_temp_config(str(config_path))
            assert not config_path.exists()
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_selected_account_auth_failure_keeps_healthy_peer_eligible():
    """Authentication damage is private to the selected binding and account."""

    from app.database import async_session, engine
    from app.services import subscription_enqueue
    from app.services.subscription_membership import recompute_subscription_membership_cache

    mark_failure = getattr(subscription_enqueue, "mark_source_auth_failure", None)
    assert mark_failure is not None, "auth failures must be recorded on private demand"

    now = datetime.now(timezone.utc)
    try:
        async with async_session() as db:
            (
                _users,
                _creator,
                subscription,
                source,
                members,
                accounts,
                bindings,
                dues,
            ) = await _seed_shared_source(db, now=now)
            accounts[0].auth_status = "healthy"
            await recompute_subscription_membership_cache(db, subscription.id)
            job = SimpleNamespace(
                subscription_source_id=source.id,
                triggering_user_subscription_id=members[0].id,
                triggering_remote_account_id=accounts[0].id,
            )

            await mark_failure(db, job, "HTTP 401 Unauthorized", when=now)

            assert bindings[0].auth_healthy is False
            assert bindings[0].auth_error_reason == "HTTP 401 Unauthorized"
            assert accounts[0].auth_status == "unhealthy"
            assert bindings[1].auth_healthy is True
            assert accounts[1].auth_status == "healthy"
            assert source.is_enabled is True
            assert source.auth_healthy is True
            assert source.next_sync_at == dues[1]
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scheduler_enqueue_selects_earliest_healthy_member_and_records_only_ids(
    monkeypatch,
):
    """A canonical enqueue must select the usable private binding, not global auth."""

    from sqlalchemy import delete, select

    from app.database import async_session, engine
    from app.models import DownloadJob
    from app.services import backpressure, download_dispatch, subscription_enqueue
    from app.services.subscription_membership import recompute_subscription_membership_cache

    @asynccontextmanager
    async def acquired_lock(*_args, **_kwargs):
        yield True

    async def no_pressure(*_args, **_kwargs):
        return None

    async def no_projection(*_args, **_kwargs):
        return None

    async def prepare(_db, job, **_kwargs):
        return SimpleNamespace(task=SimpleNamespace(id=uuid4()), job=job)

    async def publish(*_args, **_kwargs):
        return SimpleNamespace(id="rq-test")

    monkeypatch.setattr(subscription_enqueue, "redis_lock", acquired_lock)
    monkeypatch.setattr(
        subscription_enqueue,
        "get_redis",
        lambda: SimpleNamespace(hgetall=lambda _key: {}),
    )
    monkeypatch.setattr(backpressure, "download_backpressure_reason", no_pressure)
    monkeypatch.setattr(subscription_enqueue, "request_search_projection", no_projection)
    monkeypatch.setattr(download_dispatch, "prepare_download_dispatch", prepare)
    monkeypatch.setattr(download_dispatch, "publish_prepared_download", publish)

    now = datetime.now(timezone.utc)
    try:
        async with async_session() as db:
            (
                _users,
                _creator,
                subscription,
                source,
                members,
                accounts,
                _bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            await recompute_subscription_membership_cache(db, subscription.id)

            result = await subscription_enqueue.enqueue_subscription_source_sync(
                db,
                source.id,
                trigger="scheduler",
                scheduler_config={
                    "schedule_mode": "interval",
                    "default_sync_interval_hours": 6,
                    "scheduler_scan_interval_minutes": 5,
                    "timezone": "UTC",
                },
            )

            assert result["status"] == "enqueued"
            job = (
                await db.execute(
                    select(DownloadJob).where(DownloadJob.id == result["job_id"])
                )
            ).scalar_one()
            assert job.triggering_user_subscription_id == members[1].id
            assert job.triggering_remote_account_id == accounts[1].id
            assert "test-ciphertext" not in str(job.manifest)
            await db.execute(delete(DownloadJob).where(DownloadJob.id == job.id))
            await db.commit()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_shared_download_success_replans_every_member_with_its_own_schedule():
    """One shared success must move each private due time by its own interval."""

    from app.database import async_session, engine
    from app.services.subscription_enqueue import mark_source_sync_success

    when = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        async with async_session() as db:
            (
                _users,
                _creator,
                _subscription,
                source,
                _members,
                accounts,
                bindings,
                _dues,
            ) = await _seed_shared_source(db, now=when)
            accounts[0].auth_status = "healthy"
            await db.flush()

            await mark_source_sync_success(db, source.id, when)

            assert [binding.last_synced_at for binding in bindings] == [when, when]
            assert [binding.next_sync_at for binding in bindings] == [
                when + timedelta(hours=2),
                when + timedelta(hours=8),
            ]
            assert source.last_synced_at == when
            assert source.next_sync_at == when + timedelta(hours=2)
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_due_discovery_admission_claims_once_and_routes_discovery_queue():
    """Repeated scheduler admission must publish one persistent scan per account."""

    from sqlalchemy import select

    from app.database import async_session, engine
    from app.models import RemoteAccount, TaskRun, User
    from app.services import remote_discovery

    admit_due = getattr(remote_discovery, "admit_due_remote_accounts", None)
    assert admit_due is not None, "scheduler must admit due remote accounts"

    published: list[tuple[str, str]] = []

    def publish(task_id, *, queue_name):
        published.append((str(task_id), queue_name))

    now = datetime.now(timezone.utc)
    try:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
            user = User(
                username=f"shared_schedule_test_discovery_{uuid4().hex[:8]}",
                password_hash="test-only",
                is_active=True,
            )
            db.add(user)
            await db.flush()
            account = RemoteAccount(
                user_id=user.id,
                source="pixiv",
                auth_method="refresh_token",
                credential_ciphertext="test-ciphertext",
                credential_key_version=1,
                is_enabled=True,
                auth_status="healthy",
                next_scan_at=now - timedelta(minutes=1),
                scan_interval_hours=24,
            )
            db.add(account)
            await db.commit()

            first = await admit_due(db, now=now, publisher=publish)
            second = await admit_due(db, now=now, publisher=publish)

            target_tasks = list(
                (
                    await db.execute(
                        select(TaskRun).where(
                            TaskRun.triggering_remote_account_id == account.id,
                            TaskRun.operation_type == "remote-discovery-scan",
                        )
                    )
                ).scalars()
            )
            assert first["created"] >= 1
            assert second["created"] == 0
            assert len(target_tasks) == 1
            assert (str(target_tasks[0].id), "discovery") in published
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()
