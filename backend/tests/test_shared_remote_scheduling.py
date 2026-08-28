"""Shared source scheduling and private remote-account authentication."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


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
