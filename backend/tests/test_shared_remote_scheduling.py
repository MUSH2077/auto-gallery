"""Shared source scheduling and private remote-account authentication."""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FirstRowLockBarrier:
    """Pause the first locker until its peer locks or blocks on the same scope."""

    def __init__(self, *, solo_release_seconds: float = 0.25):
        self.first_arrived = asyncio.Event()
        self._release = asyncio.Event()
        self._guard = asyncio.Lock()
        self._arrivals = 0
        self._solo_release_seconds = solo_release_seconds

    async def arrive(self) -> None:
        async with self._guard:
            self._arrivals += 1
            self.first_arrived.set()
            if self._arrivals >= 2:
                self._release.set()
        try:
            await asyncio.wait_for(
                self._release.wait(), timeout=self._solo_release_seconds
            )
        except TimeoutError:
            # With a consistent lock order the peer blocks on the same first
            # row, so let the owner finish and release it.
            self._release.set()


class _FirstForUpdateSession:
    """AsyncSession proxy that delays only the first real PostgreSQL row lock."""

    def __init__(self, db, barrier: _FirstRowLockBarrier):
        self._db = db
        self._barrier = barrier
        self._paused = False

    async def execute(self, statement, *args, **kwargs):
        result = await self._db.execute(statement, *args, **kwargs)
        if (
            not self._paused
            and getattr(statement, "_for_update_arg", None) is not None
        ):
            self._paused = True
            await self._barrier.arrive()
        return result

    def __getattr__(self, name):
        return getattr(self._db, name)


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
    await db.execute(
        text(
            "DELETE FROM source_creators WHERE creator_id IN (SELECT id FROM creators "
            "WHERE name LIKE :prefix)"
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
            credential_generation=1,
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
async def test_membership_cache_keeps_canonical_schedule_constraint_consistent():
    """Canonical schedule fields remain a valid cache as private demand toggles."""

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
                _source,
                members,
                _accounts,
                _bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            for member in members:
                member.sync_enabled = False
                member.schedule_mode = "manual"

            await recompute_subscription_membership_cache(db, subscription.id)
            await db.flush()

            assert subscription.sync_enabled is False
            assert subscription.schedule_mode == "manual"

            members[1].sync_enabled = True
            members[1].schedule_mode = "interval"
            await recompute_subscription_membership_cache(db, subscription.id)
            await db.flush()

            assert subscription.sync_enabled is True
            assert subscription.schedule_mode != "manual"
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_inherited_manual_members_are_not_eligible_for_automatic_selection():
    """A system-manual inherited policy cannot trigger an automatic download."""

    from app.database import async_session, engine
    from app.services.subscription_membership import select_eligible_membership_source

    now = datetime.now(timezone.utc)
    try:
        async with async_session() as db:
            (
                _users,
                _creator,
                _subscription,
                source,
                members,
                _accounts,
                _bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            for member in members:
                member.schedule_mode = None
            await db.flush()

            selected = await select_eligible_membership_source(
                db,
                source,
                now=now,
                system_schedule_mode="manual",
            )

            assert selected is None
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_personal_download_auth_uses_0600_ephemeral_config_and_leaves_no_durable_secret(
    tmp_path,
    monkeypatch,
):
    """Credential plaintext exists only in the worker-owned 0600 temp overlay."""

    from app.database import async_session, engine
    from app.jobs import download as download_job
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter
    from app.remote_discovery.registry import DiscoveryAdapterRegistry
    from app.services import personal_auth_storage
    from app.services.remote_credentials import CredentialVault

    monkeypatch.setattr(personal_auth_storage, "_filesystem_type", lambda _path: "tmpfs")
    monkeypatch.setattr(personal_auth_storage, "_durable_roots", lambda: ())

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
                triggering_credential_generation=account.credential_generation,
                source="pixiv",
                gallerydl_config_path=None,
                manifest={"safe": True},
            )
            await db.flush()

            secret_root = tmp_path / "runtime-auth"
            personal = await materialize(
                db,
                job,
                {"extractor": {"pixiv": {"filename": "{id}.{extension}"}}},
                config_root=secret_root,
                vault=vault,
                adapters=adapters,
            )
            assert personal is not None
            config_path = personal.path
            assert config_path.parent == secret_root
            assert os.stat(secret_root).st_mode & 0o777 == 0o700
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

            # A pre-migration/unknown private job cannot be allowed to decrypt
            # whatever credential currently occupies the same account row.
            job.triggering_credential_generation = None
            stale_personal = None
            try:
                with pytest.raises(download_job.PersonalCredentialFailure):
                    stale_personal = await materialize(
                        db,
                        job,
                        {},
                        config_root=secret_root,
                        vault=vault,
                        adapters=adapters,
                    )
            finally:
                if stale_personal is not None:
                    download_job._cleanup_temp_config(str(stale_personal.path))
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_selected_account_auth_failure_keeps_healthy_peer_eligible():
    """Authentication damage is private to the selected binding and account."""

    from app.database import async_session, engine
    from app.services import subscription_enqueue
    from app.services.subscription_membership import (
        recompute_subscription_membership_cache,
        select_eligible_membership_source,
    )

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
                triggering_credential_generation=accounts[0].credential_generation,
            )

            await mark_failure(
                db,
                job,
                "HTTP 401 Unauthorized task4-reason-secret-canary",
                when=now,
            )

            assert bindings[0].auth_healthy is False
            assert bindings[0].auth_error_reason == "HTTP 401 Unauthorized"
            assert accounts[0].auth_status == "unhealthy"
            assert bindings[1].auth_healthy is True
            assert accounts[1].auth_status == "healthy"
            assert source.is_enabled is True
            assert source.auth_healthy is True
            assert source.next_sync_at == dues[1]
            takeover = await select_eligible_membership_source(db, source, now=now)
            assert takeover is not None
            assert takeover.membership.id == members[1].id
            assert takeover.account is not None
            assert takeover.account.id == accounts[1].id
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_private_auth_failure_never_poisons_canonical_source():
    """A binding reauthenticated while its old job runs makes failure provenance stale."""

    from app.database import async_session, engine
    from app.services.subscription_enqueue import mark_source_auth_failure
    from app.services.subscription_membership import recompute_subscription_membership_cache

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
                _dues,
            ) = await _seed_shared_source(db, now=now)
            accounts[0].auth_status = "healthy"
            await recompute_subscription_membership_cache(db, subscription.id)
            bindings[0].remote_account_id = None
            await recompute_subscription_membership_cache(db, subscription.id)
            job = SimpleNamespace(
                subscription_source_id=source.id,
                triggering_user_subscription_id=members[0].id,
                triggering_remote_account_id=accounts[0].id,
                triggering_credential_generation=accounts[0].credential_generation,
            )

            await mark_source_auth_failure(db, job, "HTTP 401 Unauthorized", when=now)

            assert bindings[0].auth_healthy is True
            assert accounts[0].auth_status == "healthy"
            assert source.is_enabled is True
            assert source.auth_healthy is True
            assert source.auth_status != "unhealthy"
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tombstone_delete_and_stale_success_share_one_lock_order():
    """Delete and finalization must complete without a RA/USS deadlock."""

    from sqlalchemy import select, text

    from app.database import async_session, engine
    from app.models import DiscoveryCandidate, RemoteAccount, UserSubscriptionSource
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_credentials import CredentialVault
    from app.services.subscription_enqueue import mark_source_sync_success

    now = datetime.now(timezone.utc).replace(microsecond=0)
    barrier = _FirstRowLockBarrier()
    source_id = None
    account_id = None
    binding_ids = None
    try:
        async with async_session() as db:
            (
                users,
                _creator,
                subscription,
                source,
                members,
                accounts,
                bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            for account in accounts:
                account.auth_status = "healthy"
            db.add(
                DiscoveryCandidate(
                    remote_account_id=accounts[0].id,
                    user_id=users[0].id,
                    source_creator_id=f"lock-order-{uuid4().hex}",
                    state="imported",
                    subscription_id=subscription.id,
                    user_subscription_id=members[0].id,
                )
            )
            await db.commit()
            source_id = source.id
            account_id = accounts[0].id
            member_id = members[0].id
            binding_ids = [binding.id for binding in bindings]

        vault = CredentialVault(base64.urlsafe_b64encode(b"l" * 32).decode())

        async def delete_account():
            async with async_session() as raw_db:
                await raw_db.execute(text("SET LOCAL lock_timeout = '3s'"))
                db = _FirstForUpdateSession(raw_db, barrier)
                await RemoteAccountService(db, users[0].id, vault=vault).delete(
                    account_id
                )
                await raw_db.commit()

        async def record_stale_success():
            async with async_session() as raw_db:
                await raw_db.execute(text("SET LOCAL lock_timeout = '3s'"))
                db = _FirstForUpdateSession(raw_db, barrier)
                await mark_source_sync_success(
                    db,
                    source_id,
                    now + timedelta(minutes=1),
                    triggering_user_subscription_id=member_id,
                    triggering_remote_account_id=account_id,
                    triggering_credential_generation=1,
                )
                await raw_db.commit()

        delete_task = asyncio.create_task(delete_account())
        await asyncio.wait_for(barrier.first_arrived.wait(), timeout=2)
        success_task = asyncio.create_task(record_stale_success())
        await asyncio.wait_for(
            asyncio.gather(delete_task, success_task), timeout=6
        )

        async with async_session() as db:
            account = await db.get(RemoteAccount, account_id)
            stored_bindings = list(
                (
                    await db.execute(
                        select(UserSubscriptionSource)
                        .where(UserSubscriptionSource.id.in_(binding_ids))
                        .order_by(UserSubscriptionSource.id)
                    )
                ).scalars()
            )
            deleted = next(
                binding
                for binding in stored_bindings
                if binding.remote_account_id == account_id
            )
            peer = next(binding for binding in stored_bindings if binding.id != deleted.id)
            assert account.auth_status == "deleted"
            assert account.credential_ciphertext is None
            assert deleted.auth_status == "deleted"
            assert deleted.auth_healthy is False
            assert deleted.last_synced_at is None
            assert deleted.next_sync_at is None
            assert peer.last_synced_at == now + timedelta(minutes=1)
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_validation_and_matching_auth_failure_share_one_lock_order():
    """Same-generation validation and failure serialize to one coherent state."""

    from sqlalchemy import text

    from app.database import async_session, engine
    from app.models import RemoteAccount, SubscriptionSource, UserSubscriptionSource
    from app.remote_discovery.contract import RemoteCandidateIdentity
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_credentials import CredentialVault
    from app.services.subscription_enqueue import mark_source_auth_failure

    class ValidAdapter:
        source = "pixiv"

        async def validate_account(self, credentials):
            assert credentials.materialize()["refresh_token"] == "lock-order-token"
            return RemoteCandidateIdentity(
                source="pixiv",
                source_creator_id="lock-order-user",
                profile_url="https://www.pixiv.net/users/42001",
                display_name="Lock Order User",
            )

    class ValidRegistry:
        def get(self, source):
            assert source == "pixiv"
            return ValidAdapter()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    barrier = _FirstRowLockBarrier()
    vault = CredentialVault(base64.urlsafe_b64encode(b"v" * 32).decode())
    try:
        async with async_session() as db:
            (
                users,
                _creator,
                _subscription,
                source,
                members,
                accounts,
                bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            account = accounts[0]
            account.auth_status = "healthy"
            account.credential_ciphertext = vault.encrypt(
                {"refresh_token": "lock-order-token"},
                user_id=account.user_id,
                source=account.source,
                account_id=account.id,
            )
            await db.commit()
            await db.refresh(account)
            job = SimpleNamespace(
                subscription_source_id=source.id,
                triggering_user_subscription_id=members[0].id,
                triggering_remote_account_id=account.id,
                triggering_credential_generation=account.credential_generation,
                created_at=account.updated_at,
            )
            user_id = users[0].id
            account_id = account.id
            source_id = source.id
            binding_id = bindings[0].id

        async def validate_account():
            async with async_session() as raw_db:
                await raw_db.execute(text("SET LOCAL lock_timeout = '3s'"))
                db = _FirstForUpdateSession(raw_db, barrier)
                service = RemoteAccountService(
                    db,
                    user_id,
                    vault=vault,
                    adapters=ValidRegistry(),
                )
                await service.test(account_id)
                await raw_db.commit()

        async def record_stale_failure():
            async with async_session() as raw_db:
                await raw_db.execute(text("SET LOCAL lock_timeout = '3s'"))
                db = _FirstForUpdateSession(raw_db, barrier)
                await mark_source_auth_failure(
                    db,
                    job,
                    "HTTP 401 Unauthorized",
                    when=now + timedelta(minutes=1),
                )
                await raw_db.commit()

        validation_task = asyncio.create_task(validate_account())
        await asyncio.wait_for(barrier.first_arrived.wait(), timeout=2)
        failure_task = asyncio.create_task(record_stale_failure())
        await asyncio.wait_for(
            asyncio.gather(validation_task, failure_task), timeout=6
        )

        async with async_session() as db:
            account = await db.get(RemoteAccount, account_id)
            binding = await db.get(UserSubscriptionSource, binding_id)
            source = await db.get(SubscriptionSource, source_id)
            assert account.auth_status == "unhealthy"
            assert account.auth_error_reason == "HTTP 401 Unauthorized"
            assert binding.auth_status == "unhealthy"
            assert binding.auth_healthy is False
            assert binding.auth_error_reason == "HTTP 401 Unauthorized"
            # The other user's healthy credential keeps the shared cache live.
            assert source.is_enabled is True
            assert source.auth_healthy is True
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remote_account_validation_heals_bindings_and_scheduler_cache():
    """A successful account test restores every private binding using that account."""

    from app.database import async_session, engine
    from app.remote_discovery.contract import RemoteCandidateIdentity
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_credentials import CredentialVault
    from app.services.subscription_membership import (
        recompute_subscription_membership_cache,
        select_eligible_membership_source,
    )

    class HealthyAdapter:
        async def validate_account(self, _credentials):
            return RemoteCandidateIdentity(
                source="pixiv",
                source_creator_id="42001",
                profile_url="https://www.pixiv.net/users/42001",
                display_name="Task 4",
                username="task4",
            )

    class Registry:
        def get(self, _source):
            return HealthyAdapter()

    now = datetime.now(timezone.utc)
    vault = CredentialVault(base64.urlsafe_b64encode(b"h" * 32).decode())
    try:
        async with async_session() as db:
            (
                users,
                _creator,
                subscription,
                source,
                _members,
                accounts,
                bindings,
                dues,
            ) = await _seed_shared_source(db, now=now)
            account = accounts[0]
            binding = bindings[0]
            account.credential_ciphertext = vault.encrypt(
                {"refresh_token": "task4-healed-token"},
                user_id=account.user_id,
                source=account.source,
                account_id=account.id,
            )
            binding.auth_healthy = False
            binding.auth_status = "unhealthy"
            binding.auth_error_reason = "HTTP 401 Unauthorized"
            await recompute_subscription_membership_cache(db, subscription.id)

            await RemoteAccountService(
                db,
                users[0].id,
                vault=vault,
                adapters=Registry(),
            ).test(account.id)

            assert account.auth_status == "healthy"
            assert binding.auth_healthy is True
            assert binding.auth_status == "healthy"
            assert binding.auth_error_reason is None
            assert source.is_enabled is True
            assert source.next_sync_at == dues[0]
            selected = await select_eligible_membership_source(db, source, now=now)
            assert selected is not None
            assert selected.account is not None
            assert selected.account.id == account.id
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remote_account_credential_replacement_clears_binding_failure_without_trust():
    """Replacement clears stale binding damage while the account remains untested."""

    from app.database import async_session, engine
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_credentials import CredentialVault

    now = datetime.now(timezone.utc)
    vault = CredentialVault(base64.urlsafe_b64encode(b"r" * 32).decode())
    try:
        async with async_session() as db:
            (
                users,
                _creator,
                _subscription,
                _source,
                _members,
                accounts,
                bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            account = accounts[0]
            binding = bindings[0]
            binding.auth_healthy = False
            binding.auth_status = "unhealthy"
            binding.auth_error_reason = "HTTP 401 Unauthorized"

            await RemoteAccountService(db, users[0].id, vault=vault).update(
                account.id,
                {"credentials": {"refresh_token": "task4-replacement-token"}},
            )

            assert account.auth_status == "untested"
            assert binding.auth_healthy is True
            assert binding.auth_status == "healthy"
            assert binding.auth_error_reason is None
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_personal_materialization_distinguishes_credentials_from_filesystem(
    tmp_path,
    monkeypatch,
):
    """Tampered auth is credential damage; an unavailable temp directory is not."""

    from app.database import async_session, engine
    from app.jobs import download as download_job
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter
    from app.remote_discovery.registry import DiscoveryAdapterRegistry
    from app.services import personal_auth_storage
    from app.services.remote_credentials import CredentialVault

    monkeypatch.setattr(personal_auth_storage, "_filesystem_type", lambda _path: "tmpfs")
    monkeypatch.setattr(personal_auth_storage, "_durable_roots", lambda: ())

    credential_failure = getattr(download_job, "PersonalCredentialFailure", None)
    assert credential_failure is not None
    vault = CredentialVault(base64.urlsafe_b64encode(b"m" * 32).decode())
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
            job = SimpleNamespace(
                id=uuid4(),
                subscription_id=subscription.id,
                subscription_source_id=source.id,
                triggering_user_subscription_id=members[1].id,
                triggering_remote_account_id=account.id,
                triggering_credential_generation=account.credential_generation,
                source="pixiv",
            )

            account.credential_ciphertext = "v1:tampered-task4-ciphertext"
            with pytest.raises(credential_failure):
                await download_job._materialize_personal_download_config(
                    db,
                    job,
                    {},
                    config_root=tmp_path,
                    vault=vault,
                    adapters=adapters,
                )

            account.credential_ciphertext = vault.encrypt(
                {"refresh_token": "task4-filesystem-token"},
                user_id=account.user_id,
                source=account.source,
                account_id=account.id,
            )
            monkeypatch.setattr(
                personal_auth_storage.tempfile,
                "mkstemp",
                lambda **_kwargs: (_ for _ in ()).throw(OSError("read-only filesystem")),
            )
            with pytest.raises(download_job.PersonalDownloadConfigurationError) as failure:
                await download_job._materialize_personal_download_config(
                    db,
                    job,
                    {},
                    config_root=tmp_path,
                    vault=vault,
                    adapters=adapters,
                )
            assert not isinstance(failure.value, credential_failure)
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tampered_personal_credential_fails_once_and_allows_peer_takeover(
    tmp_path,
    monkeypatch,
):
    """Pre-subprocess credential failure is terminal for only its selected provenance."""

    from app.database import async_session, engine
    from app.jobs import download as download_job
    from app.models import DownloadJob
    from app.services.remote_credentials import CredentialVault
    from app.services.subscription_membership import recompute_subscription_membership_cache

    retry_attempts: list[str] = []

    async def defaults():
        return {
            "timeout_seconds": 1,
            "stall_timeout_seconds": 1,
            "max_retries": 3,
            "max_posts": 1,
        }

    async def no_artifacts(_job_id):
        return 0, 0, []

    async def retry(job_id, **_kwargs):
        retry_attempts.append(str(job_id))

    raw_runner = download_job.run_download_job
    while hasattr(raw_runner, "__wrapped__"):
        raw_runner = raw_runner.__wrapped__
    monkeypatch.setattr(download_job, "_read_download_defaults", defaults)
    monkeypatch.setattr(download_job, "build_effective_gallerydl_config", lambda *_args: {})
    monkeypatch.setattr(download_job, "staging_enabled", lambda: False)
    monkeypatch.setattr(download_job, "_artifact_counts", no_artifacts)
    monkeypatch.setattr(download_job, "_enqueue_download_retry", retry)
    monkeypatch.setattr(download_job.settings, "download_root", str(tmp_path / "downloads"))
    monkeypatch.setattr(
        download_job.settings,
        "gallerydl_config_root",
        str(tmp_path / "gallerydl"),
    )
    monkeypatch.setattr(
        download_job.settings,
        "remote_credential_key",
        base64.urlsafe_b64encode(b"m" * 32).decode(),
    )
    (tmp_path / "downloads").mkdir()

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
            accounts[0].credential_ciphertext = "v1:tampered-task4-ciphertext"
            await recompute_subscription_membership_cache(db, subscription.id)
            job = DownloadJob(
                subscription_id=subscription.id,
                subscription_source_id=source.id,
                triggering_user_subscription_id=members[0].id,
                triggering_remote_account_id=accounts[0].id,
                triggering_credential_generation=accounts[0].credential_generation,
                source="pixiv",
                source_url=source.source_url,
                status="enqueued",
                owner_user_id=members[0].user_id,
            )
            db.add(job)
            await db.commit()
            job_id = job.id

        await raw_runner(str(job_id))

        async with async_session() as db:
            stored = await db.get(DownloadJob, job_id)
            source = await db.get(type(source), source.id)
            account_rows = [await db.get(type(account), account.id) for account in accounts]
            binding_rows = [await db.get(type(binding), binding.id) for binding in bindings]
            assert stored.status == "failed"
            assert stored.retry_count == 3
            assert retry_attempts == []
            assert account_rows[0].auth_status == "unhealthy"
            assert binding_rows[0].auth_healthy is False
            assert account_rows[1].auth_status == "healthy"
            assert binding_rows[1].auth_healthy is True
            assert source.is_enabled is True
            assert source.auth_healthy is True
            assert source.next_sync_at == dues[1]
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_shared_success_does_not_heal_an_account_outside_selected_binding():
    """Opaque provenance must match before credential health can be restored."""

    from app.database import async_session, engine
    from app.services.subscription_enqueue import mark_source_sync_success

    now = datetime.now(timezone.utc)
    try:
        async with async_session() as db:
            (
                _users,
                _creator,
                _subscription,
                source,
                members,
                accounts,
                _bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)

            await mark_source_sync_success(
                db,
                source.id,
                now,
                triggering_user_subscription_id=members[1].id,
                triggering_remote_account_id=accounts[0].id,
                triggering_credential_generation=accounts[0].credential_generation,
            )

            assert accounts[0].auth_status == "unhealthy"
            await db.rollback()
    finally:
        await engine.dispose()


def test_private_error_sanitizer_removes_secret_and_temp_path(tmp_path):
    """Unexpected worker errors cannot persist the credential config identity."""

    from app.jobs import download as download_job

    sanitize = getattr(download_job, "_private_safe_error", None)
    assert sanitize is not None
    private = download_job._PersonalDownloadConfig(
        path=tmp_path / "auth-secret.json",
        secret_values=("task4-exception-secret-canary",),
    )

    safe = sanitize(
        f"failed {private.path}: task4-exception-secret-canary",
        private,
    )

    assert "task4-exception-secret-canary" not in safe
    assert str(private.path) not in safe


def test_private_secret_collection_covers_flat_and_structured_auth_leaves():
    """Adapter auth fragments may use provider-specific keys such as flat ct0."""

    from app.jobs.download import _credential_secret_values

    flat_csrf = "task4-flat-ct0-secret-canary"
    custom_header = "task4-custom-header-secret-canary"
    nested_value = "task4-structured-auth-secret-canary"

    collected = _credential_secret_values(
        {
            "ct0": flat_csrf,
            "headers": {"X-CSRF": custom_header},
            "structured": [{"value": nested_value}],
        }
    )

    assert {flat_csrf, custom_header, nested_value} <= set(collected)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_private_auth_canary_is_redacted_and_temp_config_is_removed_after_failure(
    tmp_path,
    monkeypatch,
    caplog,
):
    """Subprocess output cannot copy credential canaries into durable/Redis/log state."""

    from sqlalchemy import select, text

    from app.database import async_session, engine
    from app.jobs import download as download_job
    from app.models import DownloadJob, TaskRun
    from app.services import job_progress, personal_auth_storage, proxy
    from app.services.remote_credentials import CredentialVault

    auth_token_canary = "task4-x-auth-token-child-canary"
    csrf_canary = "task4-x-ct0-child-canary"
    raw_cookie_canary = (
        f'auth_token="{auth_token_canary}"; ct0={csrf_canary}; '
        "guest_id=task4-x-cookie-guest-canary"
    )
    canaries = (raw_cookie_canary, auth_token_canary, csrf_canary)
    vault = CredentialVault(base64.urlsafe_b64encode(b"w" * 32).decode())
    redis_payloads: list[str] = []
    materialized_paths: list[str] = []
    process_handoffs: list[tuple[str, int]] = []

    class FakeProcess:
        pid = 987654
        returncode = 1

        def __init__(self, command, *_args, **_kwargs):
            config_index = max(
                index
                for index, item in enumerate(command)
                if item == "--config"
            )
            personal_path = command[config_index + 1]
            materialized_paths.append(personal_path)
            assert os.stat(personal_path).st_mode & 0o777 == 0o600
            private_config = Path(personal_path).read_text(encoding="utf-8")
            assert auth_token_canary in private_config
            assert csrf_canary in private_config
            self.stdout = StringIO("")
            self.stderr = StringIO(
                f"[1/2] 401 Unauthorized {raw_cookie_canary} "
                f"{auth_token_canary} {csrf_canary}\n"
            )

        def poll(self):
            return self.returncode

        def wait(self, _timeout=None):
            return self.returncode

    class FakeControl:
        command = None

        def __init__(self, *_args, proc_pid=None, pid=None):
            self.proc_pid = proc_pid
            self.pid = pid

        def detach_process(self, expected_pid):
            if self.proc_pid != expected_pid:
                return False
            self.proc_pid = None
            process_handoffs.append(("detach", expected_pid))
            return True

        def transfer_to_pid(self, pid):
            self.pid = pid
            process_handoffs.append(("heartbeat", pid))
            return True

        def start(self):
            pass

        def stop(self):
            pass

    async def defaults():
        return {
            "timeout_seconds": 1,
            "stall_timeout_seconds": 1,
            "max_retries": 3,
            "max_posts": 1,
        }

    async def no_proxy():
        return {"enabled": False}

    async def no_artifacts(_job_id):
        return 0, 0, []

    raw_runner = download_job.run_download_job
    while hasattr(raw_runner, "__wrapped__"):
        raw_runner = raw_runner.__wrapped__
    monkeypatch.setattr(download_job, "_read_download_defaults", defaults)
    monkeypatch.setattr(download_job, "build_effective_gallerydl_config", lambda *_args: {})
    monkeypatch.setattr(download_job, "staging_enabled", lambda: False)
    monkeypatch.setattr(download_job, "ControlListener", FakeControl)
    monkeypatch.setattr(download_job, "HeartbeatPublisher", FakeControl)
    monkeypatch.setattr(download_job.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(download_job, "_process_group_exists", lambda _pid: False)
    monkeypatch.setattr(download_job, "_artifact_counts", no_artifacts)
    monkeypatch.setattr(proxy, "_load_proxy_config", no_proxy)
    monkeypatch.setattr(
        job_progress.ProgressTracker,
        "set",
        staticmethod(lambda *_args: redis_payloads.append(str(_args))),
    )
    monkeypatch.setattr(
        job_progress.TaskEventPublisher,
        "publish_progress",
        staticmethod(lambda *_args: redis_payloads.append(str(_args))),
    )
    monkeypatch.setattr(
        download_job,
        "get_redis",
        lambda: SimpleNamespace(
            hset=lambda *_args, **_kwargs: None,
            expire=lambda *_args, **_kwargs: None,
        ),
    )
    monkeypatch.setattr(download_job.settings, "download_root", str(tmp_path / "downloads"))
    monkeypatch.setattr(
        download_job.settings,
        "gallerydl_config_root",
        str(tmp_path / "gallerydl"),
    )
    monkeypatch.setattr(
        download_job.settings,
        "personal_auth_tmp_root",
        str(tmp_path / "runtime-auth"),
    )
    monkeypatch.setattr(personal_auth_storage, "_filesystem_type", lambda _path: "tmpfs")
    monkeypatch.setattr(personal_auth_storage, "_durable_roots", lambda: ())
    monkeypatch.setattr(
        download_job.settings,
        "remote_credential_key",
        base64.urlsafe_b64encode(b"w" * 32).decode(),
    )
    (tmp_path / "downloads").mkdir()

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
            source.source = "x"
            source.source_url = "https://x.com/task4_artist"
            for seeded_account in accounts:
                seeded_account.source = "x"
                seeded_account.auth_method = "cookie"
            account = accounts[1]
            account.credential_ciphertext = vault.encrypt(
                {"cookie": raw_cookie_canary},
                user_id=account.user_id,
                source=account.source,
                account_id=account.id,
            )
            job = DownloadJob(
                subscription_id=subscription.id,
                subscription_source_id=source.id,
                triggering_user_subscription_id=members[1].id,
                triggering_remote_account_id=account.id,
                triggering_credential_generation=account.credential_generation,
                source="x",
                source_url=source.source_url,
                status="enqueued",
                owner_user_id=members[1].user_id,
            )
            db.add(job)
            await db.commit()
            job_id = job.id

        await raw_runner(str(job_id))

        assert process_handoffs == [("detach", FakeProcess.pid), ("heartbeat", os.getpid())]
        assert materialized_paths
        assert all(not os.path.exists(path) for path in materialized_paths)
        async with async_session() as db:
            stored = await db.get(DownloadJob, job_id)
            task = (
                await db.execute(
                    select(TaskRun).where(
                        TaskRun.subject_type == "download_job",
                        TaskRun.subject_id == job_id,
                    )
                )
            ).scalar_one()
            durable = json.dumps(
                {
                    "error": stored.error_log,
                    "manifest": stored.manifest,
                    "progress": stored.progress_data,
                    "task_error": task.error_log,
                    "task_meta": task.meta,
                    "task_progress": task.progress_data,
                },
                default=str,
            )
            assert stored.status == "failed"
            assert stored.gallerydl_config_path is None
            for canary in canaries:
                assert canary not in durable
                assert not any(canary in payload for payload in redis_payloads)
                assert canary not in caplog.text
            assert not any(path.is_file() for path in (tmp_path / "gallerydl").rglob("*"))
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
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
            assert job.triggering_credential_generation == 1
            assert "test-ciphertext" not in str(job.manifest)
            await db.execute(delete(DownloadJob).where(DownloadJob.id == job.id))
            await db.commit()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replacement_transaction_started_before_enqueue_stales_old_job_generation(
    monkeypatch,
):
    """Commit order, not transaction start time, defines credential identity."""

    from sqlalchemy import text

    from app.database import async_session, engine
    from app.models import DownloadJob, RemoteAccount, UserSubscriptionSource
    from app.services import backpressure, download_dispatch, subscription_enqueue
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_credentials import CredentialVault
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
        return SimpleNamespace(id="rq-generation-inversion")

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
    replacement_ready = asyncio.Event()
    allow_replacement = asyncio.Event()
    vault = CredentialVault(base64.urlsafe_b64encode(b"g" * 32).decode())
    source_id = None
    account_id = None
    binding_id = None
    job_id = None
    replacement_task = None
    try:
        async with async_session() as db:
            (
                users,
                _creator,
                subscription,
                source,
                _members,
                accounts,
                bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            await recompute_subscription_membership_cache(db, subscription.id)
            await db.commit()
            source_id = source.id
            account_id = accounts[1].id
            binding_id = bindings[1].id
            user_id = users[1].id

        async def replace_credentials():
            async with async_session() as raw_db:
                # Start the PostgreSQL transaction before the job exists, but
                # replace/commit after enqueue. Timestamp provenance sees the
                # earlier transaction timestamp; explicit generation sees the
                # credential identity change.
                await raw_db.execute(text("SELECT now()"))
                replacement_ready.set()
                await allow_replacement.wait()
                await RemoteAccountService(raw_db, user_id, vault=vault).update(
                    account_id,
                    {"credentials": {"refresh_token": "generation-two-token"}},
                )
                await raw_db.commit()

        replacement_task = asyncio.create_task(replace_credentials())
        await asyncio.wait_for(replacement_ready.wait(), timeout=2)

        async with async_session() as db:
            result = await subscription_enqueue.enqueue_subscription_source_sync(
                db,
                source_id,
                trigger="scheduler",
                scheduler_config={
                    "schedule_mode": "interval",
                    "default_sync_interval_hours": 6,
                    "scheduler_scan_interval_minutes": 5,
                    "timezone": "UTC",
                },
            )
            assert result["status"] == "enqueued"
            job_id = result["job_id"]

        allow_replacement.set()
        await asyncio.wait_for(replacement_task, timeout=4)

        async with async_session() as db:
            job = await db.get(DownloadJob, job_id)
            account = await db.get(RemoteAccount, account_id)
            binding = await db.get(UserSubscriptionSource, binding_id)
            assert job.triggering_credential_generation == 1
            assert account.credential_generation == 2
            assert account.auth_status == "untested"

            await subscription_enqueue.mark_source_auth_failure(
                db,
                job,
                "HTTP 401 Unauthorized",
                when=now + timedelta(minutes=1),
            )
            assert account.auth_status == "untested"
            assert binding.auth_status == "healthy"
            await db.rollback()
    finally:
        allow_replacement.set()
        if replacement_task is not None and not replacement_task.done():
            await asyncio.gather(replacement_task, return_exceptions=True)
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_owned_manual_membership_can_sync_when_automatic_cache_is_disabled(
    monkeypatch,
):
    """Explicit sync-now bypasses automatic policy, never private auth ownership."""

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
                users,
                _creator,
                subscription,
                source,
                members,
                accounts,
                bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            for member in members:
                member.sync_enabled = False
                member.schedule_mode = "manual"
            accounts[0].auth_status = "healthy"
            await recompute_subscription_membership_cache(db, subscription.id)
            assert source.is_enabled is False

            result = await subscription_enqueue.enqueue_subscription_source_sync(
                db,
                source.id,
                trigger="manual_subscription",
                triggering_user_subscription_id=members[0].id,
                triggering_remote_account_id=accounts[0].id,
            )

            assert result["status"] == "enqueued"
            job = (
                await db.execute(
                    select(DownloadJob).where(DownloadJob.id == result["job_id"])
                )
            ).scalar_one()
            assert job.triggering_user_subscription_id == members[0].id
            assert job.triggering_remote_account_id == accounts[0].id
            await db.execute(delete(DownloadJob).where(DownloadJob.id == job.id))
            await db.commit()

            denied = await subscription_enqueue.enqueue_subscription_source_sync(
                db,
                source.id,
                trigger="manual_subscription",
                triggering_user_subscription_id=members[0].id,
                triggering_remote_account_id=accounts[1].id,
            )
            assert denied["status"] == "skipped"
            assert denied["skip_reason"] == "no_eligible_member_source"

            from app.services.subscription_membership import (
                SubscriptionMembershipService,
            )

            await SubscriptionMembershipService(db, users[0].id).update_source(
                subscription.id,
                source.id,
                {"is_enabled": False},
            )
            assert bindings[0].is_enabled is False
            disabled = await subscription_enqueue.enqueue_subscription_source_sync(
                db,
                source.id,
                trigger="manual_subscription",
                triggering_user_subscription_id=members[0].id,
                triggering_remote_account_id=accounts[0].id,
            )
            assert disabled["status"] == "skipped"
            assert disabled["skip_reason"] == "no_eligible_member_source"
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_download_orchestrator_preserves_authenticated_member_over_earlier_peer(
    monkeypatch,
):
    """Source enqueue validates caller context and never substitutes an earlier peer."""

    from sqlalchemy import select

    from app.database import async_session, engine
    from app.models import DownloadJob
    from app.repositories.download_job import DownloadJobRepository
    from app.services import backpressure, download_dispatch, subscription_enqueue
    from app.services.download_orchestrator import DownloadOrchestrator
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
                users,
                _creator,
                subscription,
                source,
                members,
                accounts,
                bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            for account in accounts:
                account.auth_status = "healthy"
            bindings[0].next_sync_at = now - timedelta(minutes=1)
            bindings[1].next_sync_at = now - timedelta(days=1)
            await recompute_subscription_membership_cache(db, subscription.id)
            orchestrator = DownloadOrchestrator(db)

            result = await orchestrator.create(
                {
                    "subscription_id": subscription.id,
                    "subscription_source_id": source.id,
                    "source": source.source,
                    "source_url": source.source_url,
                    "triggering_user_subscription_id": members[0].id,
                    "triggering_remote_account_id": accounts[0].id,
                },
                DownloadJobRepository(db),
                user_id=users[0].id,
            )

            job = (
                await db.execute(
                    select(DownloadJob).where(DownloadJob.id == result["job_id"])
                )
            ).scalar_one()
            assert job.triggering_user_subscription_id == members[0].id
            assert job.triggering_remote_account_id == accounts[0].id

            with pytest.raises(ValueError, match="ownership"):
                await orchestrator.create(
                    {
                        "subscription_id": subscription.id,
                        "subscription_source_id": source.id,
                        "source": source.source,
                        "source_url": source.source_url,
                        "triggering_user_subscription_id": members[1].id,
                        "triggering_remote_account_id": accounts[1].id,
                    },
                    DownloadJobRepository(db),
                    user_id=users[0].id,
                )
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generic_manual_download_persists_caller_membership(monkeypatch):
    """A direct URL job remains private to the authenticated membership."""

    from sqlalchemy import select

    from app.database import async_session, engine
    from app.models import DownloadJob
    from app.repositories.download_job import DownloadJobRepository
    from app.services import backpressure, download_dispatch, download_orchestrator
    from app.services.download_orchestrator import DownloadOrchestrator

    async def no_pressure(*_args, **_kwargs):
        return None

    async def prepare(_db, job, **_kwargs):
        return SimpleNamespace(task=SimpleNamespace(id=uuid4()), job=job)

    async def publish(*_args, **_kwargs):
        return SimpleNamespace(id="rq-test")

    monkeypatch.setattr(backpressure, "download_backpressure_reason", no_pressure)
    monkeypatch.setattr(
        download_orchestrator,
        "download_backpressure_reason",
        no_pressure,
    )
    monkeypatch.setattr(download_dispatch, "prepare_download_dispatch", prepare)
    monkeypatch.setattr(download_dispatch, "publish_prepared_download", publish)

    now = datetime.now(timezone.utc)
    try:
        async with async_session() as db:
            (
                users,
                _creator,
                subscription,
                _source,
                members,
                _accounts,
                _bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)

            result = await DownloadOrchestrator(db).create(
                {
                    "subscription_id": subscription.id,
                    "subscription_source_id": None,
                    "source": "pixiv",
                    "source_url": "https://www.pixiv.net/users/42001",
                    "triggering_user_subscription_id": members[0].id,
                    "triggering_remote_account_id": None,
                },
                DownloadJobRepository(db),
                user_id=users[0].id,
            )

            job = (
                await db.execute(
                    select(DownloadJob).where(DownloadJob.id == result["job_id"])
                )
            ).scalar_one()
            assert job.triggering_user_subscription_id == members[0].id
            assert job.triggering_remote_account_id is None
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_shared_source_enqueues_create_one_canonical_job(monkeypatch):
    """The canonical PostgreSQL source lock serializes competing user demand."""

    from sqlalchemy import func, select

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

    async def publish(db, *_args, **_kwargs):
        # Match the real publisher's durable transaction boundary and release
        # the canonical row lock so the competing session can observe the job.
        await db.commit()
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
    source_id = None
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
            accounts[0].auth_status = "healthy"
            await recompute_subscription_membership_cache(db, subscription.id)
            await db.commit()
            source_id = source.id

        async def enqueue_once():
            async with async_session() as db:
                return await subscription_enqueue.enqueue_subscription_source_sync(
                    db,
                    source_id,
                    trigger="scheduler",
                    scheduler_config={
                        "schedule_mode": "interval",
                        "default_sync_interval_hours": 6,
                        "scheduler_scan_interval_minutes": 5,
                        "timezone": "UTC",
                    },
                )

        results = await asyncio.gather(enqueue_once(), enqueue_once())

        assert sorted(result["status"] for result in results) == ["enqueued", "skipped"]
        assert next(result for result in results if result["status"] == "skipped")[
            "skip_reason"
        ] == "already_running"
        async with async_session() as db:
            count = (
                await db.execute(
                    select(func.count(DownloadJob.id)).where(
                        DownloadJob.subscription_source_id == source_id
                    )
                )
            ).scalar_one()
            assert count == 1
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
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
async def test_shared_success_keeps_disabled_private_binding_unscheduled():
    """Shared content updates a disabled binding's receipt without creating demand."""

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
            bindings[1].is_enabled = False

            await mark_source_sync_success(db, source.id, when)

            assert bindings[0].next_sync_at == when + timedelta(hours=2)
            assert bindings[1].last_synced_at == when
            assert bindings[1].next_sync_at is None
            assert source.next_sync_at == bindings[0].next_sync_at
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_canonical_due_cache_keeps_null_when_any_eligible_member_is_unseen():
    """NULL private demand is immediately due and must win over future demand."""

    from app.database import async_session, engine
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
                accounts,
                bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            for account in accounts:
                account.auth_status = "healthy"
            bindings[0].next_sync_at = None
            bindings[1].next_sync_at = now + timedelta(hours=9)

            await recompute_subscription_membership_cache(db, subscription.id)

            assert source.is_enabled is True
            assert source.next_sync_at is None
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_private_schedule_mutations_replan_binding_and_canonical_cache(monkeypatch):
    """Interval, calendar, manual, and inherited policy replan private due rows."""

    from app.database import async_session, engine
    from app.services import subscription_membership
    from app.services.subscription_membership import SubscriptionMembershipService

    async def scheduler_config(_db):
        return {
            "schedule_mode": "interval",
            "default_sync_interval_hours": 4,
            "scheduler_scan_interval_minutes": 5,
            "timezone": "UTC",
        }

    monkeypatch.setattr(
        subscription_membership,
        "get_scheduler_config",
        scheduler_config,
        raising=False,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        async with async_session() as db:
            (
                users,
                _creator,
                subscription,
                source,
                members,
                accounts,
                bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            accounts[0].auth_status = "healthy"
            members[1].is_active = False
            bindings[0].last_synced_at = now - timedelta(hours=3)
            bindings[0].last_attempted_at = None
            bindings[0].next_sync_at = now + timedelta(days=3)
            service = SubscriptionMembershipService(db, users[0].id)

            before_interval = datetime.now(timezone.utc)
            await service.update(
                subscription.id,
                {"schedule_mode": "interval", "sync_interval_hours": 1},
            )
            assert before_interval <= bindings[0].next_sync_at <= datetime.now(timezone.utc)
            assert source.next_sync_at == bindings[0].next_sync_at

            next_hour = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(
                minute=17,
                second=0,
                microsecond=0,
            )
            await service.update(
                subscription.id,
                {
                    "schedule_mode": "calendar",
                    "schedule_rule": {
                        "frequency": "daily",
                        "times": [next_hour.strftime("%H:%M:%S")],
                    },
                },
            )
            assert bindings[0].next_sync_at is not None
            assert bindings[0].next_sync_at > datetime.now(timezone.utc)
            assert bindings[0].next_sync_at.minute == 17
            assert source.next_sync_at == bindings[0].next_sync_at

            await service.update(subscription.id, {"schedule_mode": "manual"})
            assert bindings[0].next_sync_at is None
            assert source.is_enabled is False
            assert source.next_sync_at is None

            bindings[0].last_synced_at = datetime.now(timezone.utc) - timedelta(hours=1)
            await service.update(
                subscription.id,
                {"schedule_mode": "inherit", "sync_enabled": True},
            )
            expected = bindings[0].last_synced_at + timedelta(hours=4)
            assert bindings[0].next_sync_at == expected
            assert source.is_enabled is True
            assert source.next_sync_at == expected
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_system_schedule_change_replans_inherited_private_bindings():
    """Inherited private rows, rather than the canonical cache, own system replans."""

    from app.database import async_session, engine
    from app.services.subscription_replan import replan_inherited_subscription_sources

    now = datetime.now(timezone.utc).replace(microsecond=0)
    config = {
        "schedule_mode": "interval",
        "default_sync_interval_hours": 1,
        "scheduler_scan_interval_minutes": 5,
        "timezone": "UTC",
    }
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
                _dues,
            ) = await _seed_shared_source(db, now=now)
            for account in accounts:
                account.auth_status = "healthy"
            subscription.schedule_mode = None
            for index, (member, binding) in enumerate(zip(members, bindings, strict=True)):
                member.schedule_mode = None
                member.sync_interval_hours = 9
                binding.last_synced_at = now - timedelta(minutes=30 + index)
                binding.last_attempted_at = None
                binding.next_sync_at = now + timedelta(days=7)

            replanned = await replan_inherited_subscription_sources(db, config, now=now)

            expected = [
                bindings[0].last_synced_at + timedelta(hours=1),
                bindings[1].last_synced_at + timedelta(hours=1),
            ]
            assert replanned >= 2
            assert [binding.next_sync_at for binding in bindings] == expected
            assert source.next_sync_at == min(expected)
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fast_success_finalization_wins_over_enqueue_claim(monkeypatch):
    """A worker finishing before publish returns cannot be overwritten by enqueue."""

    from sqlalchemy import select

    from app.database import async_session, engine
    from app.models import UserSubscriptionSource
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

    completed_at = datetime.now(timezone.utc).replace(microsecond=0)

    async def publish(db, job, *_args, **_kwargs):
        await db.commit()
        async with async_session() as worker_db:
            await subscription_enqueue.mark_source_sync_success(
                worker_db,
                job.subscription_source_id,
                completed_at,
                triggering_user_subscription_id=job.triggering_user_subscription_id,
                triggering_remote_account_id=job.triggering_remote_account_id,
                triggering_credential_generation=(
                    job.triggering_credential_generation
                ),
            )
            await worker_db.commit()
        return SimpleNamespace(id="rq-fast-success")

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

    source_id = None
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
            ) = await _seed_shared_source(db, now=completed_at)
            for account in accounts:
                account.auth_status = "healthy"
            await recompute_subscription_membership_cache(db, subscription.id)
            await db.commit()
            source_id = source.id

        async with async_session() as db:
            result = await subscription_enqueue.enqueue_subscription_source_sync(
                db,
                source_id,
                trigger="scheduler",
                scheduler_config={
                    "schedule_mode": "interval",
                    "default_sync_interval_hours": 6,
                    "scheduler_scan_interval_minutes": 5,
                    "timezone": "UTC",
                },
            )
            assert result["status"] == "enqueued"

        async with async_session() as db:
            stored = list(
                (
                    await db.execute(
                        select(UserSubscriptionSource)
                        .where(UserSubscriptionSource.subscription_source_id == source_id)
                        .order_by(UserSubscriptionSource.id)
                    )
                ).scalars()
            )
            assert sorted(binding.next_sync_at for binding in stored) == [
                completed_at + timedelta(hours=2),
                completed_at + timedelta(hours=8),
            ]
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_publication_failure_restores_original_private_demand(monkeypatch):
    """A rejected publication must not consume the selected member's due slot."""

    from sqlalchemy import select

    from app.database import async_session, engine
    from app.models import SubscriptionSource, UserSubscriptionSource
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

    async def reject_after_durable_claim(db, *_args, **_kwargs):
        await db.commit()
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(subscription_enqueue, "redis_lock", acquired_lock)
    monkeypatch.setattr(
        subscription_enqueue,
        "get_redis",
        lambda: SimpleNamespace(hgetall=lambda _key: {}),
    )
    monkeypatch.setattr(backpressure, "download_backpressure_reason", no_pressure)
    monkeypatch.setattr(subscription_enqueue, "request_search_projection", no_projection)
    monkeypatch.setattr(download_dispatch, "prepare_download_dispatch", prepare)
    monkeypatch.setattr(
        download_dispatch,
        "publish_prepared_download",
        reject_after_durable_claim,
    )

    now = datetime.now(timezone.utc).replace(microsecond=0)
    source_id = None
    original_dues = None
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
                dues,
            ) = await _seed_shared_source(db, now=now)
            for account in accounts:
                account.auth_status = "healthy"
            await recompute_subscription_membership_cache(db, subscription.id)
            await db.commit()
            source_id = source.id
            original_dues = list(dues)

        async with async_session() as db:
            result = await subscription_enqueue.enqueue_subscription_source_sync(
                db,
                source_id,
                trigger="scheduler",
                scheduler_config={
                    "schedule_mode": "interval",
                    "default_sync_interval_hours": 6,
                    "scheduler_scan_interval_minutes": 5,
                    "timezone": "UTC",
                },
            )
            assert result["status"] == "error"

        async with async_session() as db:
            stored_source = await db.get(SubscriptionSource, source_id)
            stored_bindings = list(
                (
                    await db.execute(
                        select(UserSubscriptionSource)
                        .where(UserSubscriptionSource.subscription_source_id == source_id)
                        .order_by(UserSubscriptionSource.id)
                    )
                ).scalars()
            )
            assert sorted(binding.next_sync_at for binding in stored_bindings) == sorted(
                original_dues
            )
            assert stored_source.next_sync_at == min(original_dues)
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_publication_restore_preserves_concurrent_binding_disable_policy():
    """A failed publish cannot restore demand across a binding policy edit."""

    from sqlalchemy import text

    from app.database import async_session, engine
    from app.models import SubscriptionSource, UserSubscriptionSource
    from app.services import subscription_enqueue
    from app.services.subscription_membership import SubscriptionMembershipService

    now = datetime.now(timezone.utc).replace(microsecond=0)
    claimed_next = now + timedelta(minutes=5)
    barrier = _FirstRowLockBarrier()
    try:
        async with async_session() as db:
            (
                users,
                _creator,
                _subscription,
                source,
                _members,
                accounts,
                bindings,
                dues,
            ) = await _seed_shared_source(db, now=now)
            accounts[0].auth_status = "healthy"
            selected = bindings[0]
            previous_source_attempted_at = source.last_attempted_at
            previous_binding_attempted_at = selected.last_attempted_at
            previous_binding_next_sync_at = selected.next_sync_at
            source.last_attempted_at = now
            selected.last_attempted_at = now
            selected.next_sync_at = claimed_next
            await db.commit()
            await db.refresh(selected)
            await db.refresh(accounts[0])
            claimed_binding_updated_at = selected.updated_at
            claimed_binding_is_enabled = selected.is_enabled
            claimed_binding_auth_healthy = selected.auth_healthy
            claimed_binding_auth_status = selected.auth_status
            claimed_account_auth_status = accounts[0].auth_status
            source_id = source.id
            subscription_id = source.subscription_id
            binding_id = selected.id
            membership_id = selected.user_subscription_id
            remote_account_id = selected.remote_account_id
            credential_generation = accounts[0].credential_generation
            user_id = users[0].id

        async def restore_failed_publication():
            async with async_session() as raw_db:
                await raw_db.execute(text("SET lock_timeout = '3s'"))
                db = _FirstForUpdateSession(raw_db, barrier)
                await subscription_enqueue._restore_failed_publication_demand(
                    db,
                    source_id=source_id,
                    binding_id=binding_id,
                    membership_id=membership_id,
                    remote_account_id=remote_account_id,
                    credential_generation=credential_generation,
                    claimed_at=now,
                    claimed_next_sync_at=claimed_next,
                    claimed_binding_updated_at=claimed_binding_updated_at,
                    claimed_binding_is_enabled=claimed_binding_is_enabled,
                    claimed_binding_auth_healthy=claimed_binding_auth_healthy,
                    claimed_binding_auth_status=claimed_binding_auth_status,
                    claimed_account_auth_status=claimed_account_auth_status,
                    previous_source_attempted_at=previous_source_attempted_at,
                    previous_binding_attempted_at=previous_binding_attempted_at,
                    previous_binding_next_sync_at=previous_binding_next_sync_at,
                )

        async def edit_binding_policy():
            async with async_session() as raw_db:
                await raw_db.execute(text("SET lock_timeout = '3s'"))
                db = _FirstForUpdateSession(raw_db, barrier)
                service = SubscriptionMembershipService(db, user_id)
                await service.update_source(
                    subscription_id,
                    source_id,
                    {"is_enabled": False},
                )
                await raw_db.commit()

        restore_task = asyncio.create_task(restore_failed_publication())
        await asyncio.wait_for(barrier.first_arrived.wait(), timeout=2)
        edit_task = asyncio.create_task(edit_binding_policy())
        await asyncio.wait_for(asyncio.gather(restore_task, edit_task), timeout=6)

        async with async_session() as db:
            binding = await db.get(UserSubscriptionSource, binding_id)
            stored_source = await db.get(SubscriptionSource, source_id)
            assert binding.remote_account_id == remote_account_id
            assert binding.is_enabled is False
            assert binding.next_sync_at is None
            assert binding.next_sync_at != dues[0]
            assert stored_source.next_sync_at != dues[0]
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_publication_restore_does_not_cross_disable_reenable_transition():
    """A completed policy cycle invalidates the old claim even if due matches."""

    from app.database import async_session, engine
    from app.models import UserSubscriptionSource
    from app.services import subscription_enqueue
    from app.services.subscription_membership import SubscriptionMembershipService

    now = datetime.now(timezone.utc).replace(microsecond=0)
    claimed_next = now + timedelta(minutes=5)
    try:
        async with async_session() as db:
            (
                users,
                _creator,
                _subscription,
                source,
                _members,
                accounts,
                bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            accounts[0].auth_status = "healthy"
            selected = bindings[0]
            previous_source_attempted_at = source.last_attempted_at
            previous_binding_attempted_at = selected.last_attempted_at
            previous_binding_next_sync_at = selected.next_sync_at
            source.last_attempted_at = now
            selected.last_attempted_at = now
            selected.next_sync_at = claimed_next
            await db.commit()
            await db.refresh(selected)
            await db.refresh(accounts[0])
            claimed_binding_updated_at = selected.updated_at
            claimed_binding_is_enabled = selected.is_enabled
            claimed_binding_auth_healthy = selected.auth_healthy
            claimed_binding_auth_status = selected.auth_status
            claimed_account_auth_status = accounts[0].auth_status
            source_id = source.id
            subscription_id = source.subscription_id
            binding_id = selected.id
            membership_id = selected.user_subscription_id
            remote_account_id = selected.remote_account_id
            credential_generation = accounts[0].credential_generation
            user_id = users[0].id

        async with async_session() as db:
            service = SubscriptionMembershipService(db, user_id)
            await service.update_source(
                subscription_id,
                source_id,
                {"is_enabled": False},
            )
            await service.update_source(
                subscription_id,
                source_id,
                {"is_enabled": True},
            )
            rebound = await db.get(UserSubscriptionSource, binding_id)
            replanned_next = rebound.next_sync_at
            await db.commit()

        async with async_session() as db:
            await subscription_enqueue._restore_failed_publication_demand(
                db,
                source_id=source_id,
                binding_id=binding_id,
                membership_id=membership_id,
                remote_account_id=remote_account_id,
                credential_generation=credential_generation,
                claimed_at=now,
                claimed_next_sync_at=claimed_next,
                claimed_binding_updated_at=claimed_binding_updated_at,
                claimed_binding_is_enabled=claimed_binding_is_enabled,
                claimed_binding_auth_healthy=claimed_binding_auth_healthy,
                claimed_binding_auth_status=claimed_binding_auth_status,
                claimed_account_auth_status=claimed_account_auth_status,
                previous_source_attempted_at=previous_source_attempted_at,
                previous_binding_attempted_at=previous_binding_attempted_at,
                previous_binding_next_sync_at=previous_binding_next_sync_at,
            )

        async with async_session() as db:
            binding = await db.get(UserSubscriptionSource, binding_id)
            assert binding.is_enabled is True
            assert binding.next_sync_at == replanned_next
            assert binding.next_sync_at != previous_binding_next_sync_at
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_binding_disable_then_enable_replans_current_member_schedule():
    """Re-enabling creates fresh demand instead of reviving an overdue slot."""

    from app.database import async_session, engine
    from app.services.subscription_membership import (
        SubscriptionMembershipService,
        recompute_subscription_membership_cache,
        select_eligible_membership_source,
    )

    now = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        async with async_session() as db:
            (
                users,
                _creator,
                subscription,
                source,
                members,
                accounts,
                bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            accounts[0].auth_status = "healthy"
            bindings[0].last_synced_at = now
            bindings[0].next_sync_at = now - timedelta(days=1)
            bindings[1].is_enabled = False
            bindings[1].next_sync_at = None
            await recompute_subscription_membership_cache(db, subscription.id)
            await db.commit()

            service = SubscriptionMembershipService(db, users[0].id)
            await service.update_source(
                subscription.id,
                source.id,
                {"is_enabled": False},
            )
            assert bindings[0].next_sync_at is None
            assert source.is_enabled is False
            assert (
                await select_eligible_membership_source(
                    db,
                    source,
                    now=now,
                    preferred_membership_id=members[0].id,
                    preferred_account_id=accounts[0].id,
                    require_due=False,
                    require_sync_enabled=False,
                )
                is None
            )

            enabled_at = datetime.now(timezone.utc)
            await service.update_source(
                subscription.id,
                source.id,
                {"is_enabled": True},
            )
            assert bindings[0].next_sync_at > enabled_at
            assert bindings[0].next_sync_at >= enabled_at + timedelta(hours=1)
            assert source.is_enabled is True
            assert source.next_sync_at == bindings[0].next_sync_at
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_publication_restore_and_member_rebind_share_lifecycle_lock_order():
    """Restore must not hold canonical rows while waiting for a member edit."""

    from sqlalchemy import text

    from app.database import async_session, engine
    from app.services import subscription_enqueue
    from app.services.subscription_membership import SubscriptionMembershipService

    now = datetime.now(timezone.utc).replace(microsecond=0)
    claimed_next = now + timedelta(hours=2)
    barrier = _FirstRowLockBarrier()
    try:
        async with async_session() as db:
            (
                users,
                _creator,
                _subscription,
                source,
                _members,
                accounts,
                bindings,
                dues,
            ) = await _seed_shared_source(db, now=now)
            accounts[0].auth_status = "healthy"
            selected = bindings[0]
            previous_source_attempted_at = source.last_attempted_at
            previous_binding_attempted_at = selected.last_attempted_at
            previous_binding_next_sync_at = selected.next_sync_at
            source.last_attempted_at = now
            selected.last_attempted_at = now
            selected.next_sync_at = claimed_next
            await db.commit()
            await db.refresh(selected)
            await db.refresh(accounts[0])
            claimed_binding_updated_at = selected.updated_at
            claimed_binding_is_enabled = selected.is_enabled
            claimed_binding_auth_healthy = selected.auth_healthy
            claimed_binding_auth_status = selected.auth_status
            claimed_account_auth_status = accounts[0].auth_status
            source_id = source.id
            subscription_id = source.subscription_id
            binding_id = selected.id
            membership_id = selected.user_subscription_id
            remote_account_id = selected.remote_account_id
            credential_generation = accounts[0].credential_generation
            user_id = users[0].id

        async def restore_failed_publication():
            async with async_session() as raw_db:
                await raw_db.execute(text("SET lock_timeout = '3s'"))
                db = _FirstForUpdateSession(raw_db, barrier)
                await subscription_enqueue._restore_failed_publication_demand(
                    db,
                    source_id=source_id,
                    binding_id=binding_id,
                    membership_id=membership_id,
                    remote_account_id=remote_account_id,
                    credential_generation=credential_generation,
                    claimed_at=now,
                    claimed_next_sync_at=claimed_next,
                    claimed_binding_updated_at=claimed_binding_updated_at,
                    claimed_binding_is_enabled=claimed_binding_is_enabled,
                    claimed_binding_auth_healthy=claimed_binding_auth_healthy,
                    claimed_binding_auth_status=claimed_binding_auth_status,
                    claimed_account_auth_status=claimed_account_auth_status,
                    previous_source_attempted_at=previous_source_attempted_at,
                    previous_binding_attempted_at=previous_binding_attempted_at,
                    previous_binding_next_sync_at=previous_binding_next_sync_at,
                )

        async def detach_member_auth():
            async with async_session() as raw_db:
                await raw_db.execute(text("SET lock_timeout = '3s'"))
                db = _FirstForUpdateSession(raw_db, barrier)
                await SubscriptionMembershipService(db, user_id).update_source(
                    subscription_id,
                    source_id,
                    {"remote_account_id": None, "is_enabled": False},
                )
                await raw_db.commit()

        restore_task = asyncio.create_task(restore_failed_publication())
        await asyncio.wait_for(barrier.first_arrived.wait(), timeout=2)
        edit_task = asyncio.create_task(detach_member_auth())
        await asyncio.wait_for(asyncio.gather(restore_task, edit_task), timeout=6)

        async with async_session() as db:
            binding = await db.get(type(bindings[0]), binding_id)
            assert binding.remote_account_id is None
            assert binding.is_enabled is False
            assert binding.next_sync_at is None
            assert binding.next_sync_at != dues[0]
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_account_delete_and_member_rebind_lock_remote_account_first():
    """Delete and rebind serialize at RemoteAccount without a USS lock cycle."""

    from sqlalchemy import text

    from app.database import async_session, engine
    from app.models import RemoteAccount, SubscriptionSource, UserSubscriptionSource
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_credentials import CredentialVault
    from app.services.subscription_membership import SubscriptionMembershipService

    now = datetime.now(timezone.utc).replace(microsecond=0)
    delete_locked_account = asyncio.Event()
    allow_delete = asyncio.Event()
    rebind_attempted_account = asyncio.Event()
    rebind_locked_binding = asyncio.Event()
    vault = CredentialVault(base64.urlsafe_b64encode(b"r" * 32).decode())

    class DeletePauseSession:
        def __init__(self, db):
            self._db = db
            self._paused = False

        async def execute(self, statement, *args, **kwargs):
            result = await self._db.execute(statement, *args, **kwargs)
            if not self._paused and getattr(statement, "_for_update_arg", None) is not None:
                self._paused = True
                delete_locked_account.set()
                await allow_delete.wait()
            return result

        def __getattr__(self, name):
            return getattr(self._db, name)

    class RebindWatchSession:
        def __init__(self, db):
            self._db = db

        async def execute(self, statement, *args, **kwargs):
            entities = {
                item.get("entity")
                for item in getattr(statement, "column_descriptions", ())
            }
            if (
                getattr(statement, "_for_update_arg", None) is not None
                and RemoteAccount in entities
            ):
                rebind_attempted_account.set()
            result = await self._db.execute(statement, *args, **kwargs)
            if (
                getattr(statement, "_for_update_arg", None) is not None
                and UserSubscriptionSource in entities
            ):
                rebind_locked_binding.set()
            return result

        def __getattr__(self, name):
            return getattr(self._db, name)

    try:
        async with async_session() as db:
            (
                users,
                _creator,
                subscription,
                source,
                _members,
                accounts,
                bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            account = accounts[0]
            account.auth_status = "healthy"
            await db.commit()
            user_id = users[0].id
            subscription_id = subscription.id
            source_id = source.id
            binding_id = bindings[0].id
            account_id = account.id

        async def delete_account():
            async with async_session() as raw_db:
                await raw_db.execute(text("SET lock_timeout = '3s'"))
                db = DeletePauseSession(raw_db)
                await RemoteAccountService(db, user_id, vault=vault).delete(account_id)
                await raw_db.commit()

        async def rebind_same_account():
            async with async_session() as raw_db:
                await raw_db.execute(text("SET lock_timeout = '3s'"))
                db = RebindWatchSession(raw_db)
                try:
                    await SubscriptionMembershipService(db, user_id).update_source(
                        subscription_id,
                        source_id,
                        {"remote_account_id": account_id},
                    )
                except ValueError as exc:
                    await raw_db.rollback()
                    return str(exc)
                await raw_db.commit()
                return None

        delete_task = asyncio.create_task(delete_account())
        await asyncio.wait_for(delete_locked_account.wait(), timeout=2)
        rebind_task = asyncio.create_task(rebind_same_account())
        await asyncio.wait_for(rebind_attempted_account.wait(), timeout=2)
        binding_locked_before_account_release = rebind_locked_binding.is_set()
        allow_delete.set()
        _, rebind_error = await asyncio.wait_for(
            asyncio.gather(delete_task, rebind_task), timeout=6
        )
        assert binding_locked_before_account_release is False
        assert rebind_error == "Remote account not found"

        async with async_session() as db:
            assert await db.get(RemoteAccount, account_id) is None
            binding = await db.get(UserSubscriptionSource, binding_id)
            canonical = await db.get(SubscriptionSource, source_id)
            assert binding.remote_account_id is None
            assert binding.auth_status == "deleted"
            assert binding.next_sync_at is None
            assert canonical.is_enabled is True
    finally:
        allow_delete.set()
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_member_attach_accepts_only_owned_current_provider_account():
    """Attach rejects cross-owner/source, tombstoned, and credential-less accounts."""

    from app.database import async_session, engine
    from app.models import RemoteAccount
    from app.services.subscription_membership import SubscriptionMembershipService

    now = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        async with async_session() as db:
            (
                users,
                _creator,
                subscription,
                source,
                _members,
                accounts,
                bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            accounts[0].auth_status = "healthy"
            valid_id = accounts[0].id
            binding = bindings[0]
            binding.remote_account_id = None
            wrong_source = RemoteAccount(
                user_id=users[0].id,
                source="x",
                auth_method="cookie",
                credential_ciphertext="test-wrong-source-ciphertext",
                credential_key_version=1,
                credential_generation=1,
                is_enabled=True,
                auth_status="healthy",
            )
            db.add(wrong_source)
            await db.commit()

            service = SubscriptionMembershipService(db, users[0].id)
            await service.update_source(
                subscription.id,
                source.id,
                {"remote_account_id": valid_id},
            )
            assert binding.remote_account_id == valid_id

            with pytest.raises(ValueError, match="Remote account not found"):
                await SubscriptionMembershipService(db, users[1].id).update_source(
                    subscription.id,
                    source.id,
                    {"remote_account_id": valid_id},
                )
            with pytest.raises(ValueError, match="source does not match"):
                await service.update_source(
                    subscription.id,
                    source.id,
                    {"remote_account_id": wrong_source.id},
                )
            await service.update_source(
                subscription.id,
                source.id,
                {"remote_account_id": None},
            )
            accounts[0].is_enabled = False
            accounts[0].auth_status = "deleted"
            accounts[0].credential_ciphertext = None
            accounts[0].credential_generation += 1
            await db.commit()
            with pytest.raises(ValueError, match="Remote account is not usable"):
                await service.update_source(
                    subscription.id,
                    source.id,
                    {"remote_account_id": valid_id},
                )

            accounts[0].is_enabled = True
            accounts[0].auth_status = "healthy"
            await db.commit()
            with pytest.raises(ValueError, match="Remote account is not usable"):
                await service.update_source(
                    subscription.id,
                    source.id,
                    {"remote_account_id": valid_id},
                )
            assert binding.remote_account_id is None
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_source_rebind_resets_only_target_health_and_is_immediately_eligible():
    """A healthy destination must replace stale auth state on only one binding."""

    from app.database import async_session, engine
    from app.models import RemoteAccount, SubscriptionSource, UserSubscriptionSource
    from app.services.subscription_membership import (
        SubscriptionMembershipService,
        recompute_subscription_membership_cache,
        select_eligible_membership_source,
    )

    now = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        async with async_session() as db:
            (
                users,
                _creator,
                subscription,
                source,
                members,
                accounts,
                bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            destination = accounts[0]
            destination.auth_status = "healthy"
            destination.auth_error_reason = None
            destination.last_authenticated_at = now
            old_account = RemoteAccount(
                user_id=users[0].id,
                source="x",
                auth_method="cookie",
                credential_ciphertext="test-stale-account-ciphertext",
                credential_key_version=1,
                credential_generation=1,
                is_enabled=False,
                auth_status="deleted",
                auth_error_reason="old account deleted",
            )
            other_source = SubscriptionSource(
                subscription_id=subscription.id,
                source="x",
                source_url=f"https://x.com/stale_{uuid4().hex}",
                is_enabled=True,
            )
            db.add_all([old_account, other_source])
            await db.flush()
            target = bindings[0]
            target.remote_account_id = old_account.id
            target.auth_healthy = False
            target.auth_status = "deleted"
            target.auth_error_reason = "old account deleted"
            target.last_auth_checked_at = now - timedelta(days=2)
            unrelated = UserSubscriptionSource(
                user_id=users[0].id,
                subscription_id=subscription.id,
                user_subscription_id=members[0].id,
                subscription_source_id=other_source.id,
                remote_account_id=old_account.id,
                is_enabled=True,
                auth_healthy=False,
                auth_status="deleted",
                auth_error_reason="unrelated old failure",
                last_auth_checked_at=now - timedelta(days=3),
            )
            db.add(unrelated)
            await recompute_subscription_membership_cache(db, subscription.id)

            await SubscriptionMembershipService(db, users[0].id).update_source(
                subscription.id,
                source.id,
                {"remote_account_id": destination.id},
            )

            assert target.remote_account_id == destination.id
            assert target.auth_healthy is True
            assert target.auth_status == "healthy"
            assert target.auth_error_reason is None
            assert target.last_auth_checked_at == now
            assert unrelated.remote_account_id == old_account.id
            assert unrelated.auth_healthy is False
            assert unrelated.auth_status == "deleted"
            assert unrelated.auth_error_reason == "unrelated old failure"
            selected = await select_eligible_membership_source(
                db,
                source,
                now=now,
                preferred_membership_id=members[0].id,
                preferred_account_id=destination.id,
                require_due=False,
            )
            assert selected is not None
            assert selected.binding.id == target.id
            assert selected.account.id == destination.id
            assert source.is_enabled is True
            assert source.auth_healthy is True
            await db.rollback()
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_source_explicit_detach_restores_public_auth_eligibility():
    """Explicit detach cannot retain a failed personal-account health state."""

    from app.database import async_session, engine
    from app.services.subscription_membership import (
        SubscriptionMembershipService,
        recompute_subscription_membership_cache,
        select_eligible_membership_source,
    )

    now = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        async with async_session() as db:
            (
                users,
                _creator,
                subscription,
                source,
                members,
                _accounts,
                bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            target = bindings[0]
            target.auth_healthy = False
            target.auth_status = "unhealthy"
            target.auth_error_reason = "personal token expired"
            target.last_auth_checked_at = now - timedelta(days=1)
            await recompute_subscription_membership_cache(db, subscription.id)

            await SubscriptionMembershipService(db, users[0].id).update_source(
                subscription.id,
                source.id,
                {"remote_account_id": None},
            )

            assert target.remote_account_id is None
            assert target.auth_healthy is True
            assert target.auth_status == "healthy"
            assert target.auth_error_reason is None
            assert target.last_auth_checked_at is None
            selected = await select_eligible_membership_source(
                db,
                source,
                now=now,
                preferred_membership_id=members[0].id,
                require_due=False,
                require_preferred_account_match=True,
            )
            assert selected is not None
            assert selected.binding.id == target.id
            assert selected.account is None
            assert source.is_enabled is True
            assert source.auth_healthy is True
            await db.rollback()
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_candidate_import_rebind_resets_existing_binding_health():
    """The ensure/import path resets stale auth when adopting its locked account."""

    from app.database import async_session, engine
    from app.models import DiscoveryCandidate, RemoteAccount
    from app.services.remote_credentials import CredentialVault
    from app.services.remote_discovery import RemoteDiscoveryService
    from app.services.subscription_membership import (
        recompute_subscription_membership_cache,
        select_eligible_membership_source,
    )

    now = datetime.now(timezone.utc).replace(microsecond=0)
    vault = CredentialVault(base64.urlsafe_b64encode(b"i" * 32).decode())
    try:
        async with async_session() as db:
            (
                users,
                creator,
                subscription,
                source,
                members,
                accounts,
                bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            destination = accounts[0]
            destination.auth_status = "healthy"
            destination.auth_error_reason = None
            destination.last_authenticated_at = now
            destination.credential_ciphertext = vault.encrypt(
                {"refresh_token": "test-import-rebind-token"},
                user_id=users[0].id,
                source="pixiv",
                account_id=destination.id,
            )
            old_account = RemoteAccount(
                user_id=users[0].id,
                source="x",
                auth_method="cookie",
                credential_ciphertext="test-import-old-ciphertext",
                credential_key_version=1,
                credential_generation=1,
                is_enabled=False,
                auth_status="deleted",
            )
            db.add(old_account)
            await db.flush()
            remote_creator_id = f"import-rebind-{uuid4().hex}"
            remote_url = f"https://www.pixiv.net/users/{uuid4().int % 1000000}"
            source.source_creator_id = remote_creator_id
            source.source_url = remote_url
            target = bindings[0]
            target.remote_account_id = old_account.id
            target.auth_healthy = False
            target.auth_status = "deleted"
            target.auth_error_reason = "stale import binding"
            target.last_auth_checked_at = now - timedelta(days=4)
            candidate = DiscoveryCandidate(
                remote_account_id=destination.id,
                user_id=users[0].id,
                source_creator_id=remote_creator_id,
                remote_url=remote_url,
                display_name="Import Rebind",
                state="pending",
                candidate_metadata={"local_creator_ids": [str(creator.id)]},
            )
            db.add(candidate)
            await recompute_subscription_membership_cache(db, subscription.id)
            await db.flush()

            imported = await RemoteDiscoveryService(
                db,
                vault=vault,
            ).import_candidate(users[0].id, candidate.id)

            assert imported.state == "imported"
            assert target.remote_account_id == destination.id
            assert target.auth_healthy is True
            assert target.auth_status == "healthy"
            assert target.auth_error_reason is None
            assert target.last_auth_checked_at == now
            selected = await select_eligible_membership_source(
                db,
                source,
                now=now,
                preferred_membership_id=members[0].id,
                preferred_account_id=destination.id,
                require_due=False,
            )
            assert selected is not None
            assert selected.binding.id == target.id
            assert source.is_enabled is True
            assert source.auth_healthy is True
            await db.rollback()
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_candidate_import_and_account_delete_serialize_without_inversion():
    """Import waits on RemoteAccount before Candidate or a new/NULL binding."""

    from sqlalchemy import text

    from app.database import async_session, engine
    from app.models import (
        DiscoveryCandidate,
        RemoteAccount,
        SourceCreator,
        UserSubscriptionSource,
    )
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_credentials import CredentialVault
    from app.services.remote_discovery import RemoteDiscoveryService

    now = datetime.now(timezone.utc).replace(microsecond=0)
    marker = f"candidate-delete-lock-{uuid4().hex}"
    delete_locked_account = asyncio.Event()
    allow_delete = asyncio.Event()
    import_attempted_account = asyncio.Event()
    import_attempted_later_lock = asyncio.Event()

    class DeletePauseSession:
        def __init__(self, db):
            self._db = db
            self._paused = False

        async def execute(self, statement, *args, **kwargs):
            result = await self._db.execute(statement, *args, **kwargs)
            if not self._paused and getattr(statement, "_for_update_arg", None) is not None:
                self._paused = True
                delete_locked_account.set()
                await allow_delete.wait()
            return result

        def __getattr__(self, name):
            return getattr(self._db, name)

    class ImportOrderSession:
        def __init__(self, db):
            self._db = db

        async def execute(self, statement, *args, **kwargs):
            entities = {
                item.get("entity")
                for item in getattr(statement, "column_descriptions", ())
            }
            if getattr(statement, "_for_update_arg", None) is not None:
                if RemoteAccount in entities:
                    import_attempted_account.set()
                elif DiscoveryCandidate in entities or UserSubscriptionSource in entities:
                    import_attempted_later_lock.set()
            return await self._db.execute(statement, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._db, name)

    try:
        async with async_session() as db:
            (
                users,
                creator,
                _subscription,
                source,
                _members,
                accounts,
                bindings,
                _dues,
            ) = await _seed_shared_source(db, now=now)
            account = accounts[0]
            account.auth_status = "healthy"
            bindings[0].remote_account_id = None
            source.source_creator_id = marker
            db.add(
                SourceCreator(
                    creator_id=creator.id,
                    source="pixiv",
                    source_creator_id=marker,
                    source_url=source.source_url,
                    display_name=creator.name,
                )
            )
            candidate = DiscoveryCandidate(
                remote_account_id=account.id,
                user_id=users[0].id,
                source_creator_id=marker,
                remote_url=source.source_url,
                display_name=creator.name,
                confidence="high",
                state="pending",
                is_following=True,
            )
            db.add(candidate)
            await db.commit()
            account_id = account.id
            candidate_id = candidate.id
            user_id = users[0].id
            binding_id = bindings[0].id

        vault = CredentialVault(base64.urlsafe_b64encode(b"d" * 32).decode())

        async def delete_account():
            async with async_session() as raw_db:
                await raw_db.execute(text("SET lock_timeout = '3s'"))
                db = DeletePauseSession(raw_db)
                await RemoteAccountService(db, user_id, vault=vault).delete(account_id)
                await raw_db.commit()

        async def import_candidate():
            async with async_session() as raw_db:
                await raw_db.execute(text("SET lock_timeout = '3s'"))
                db = ImportOrderSession(raw_db)
                try:
                    await RemoteDiscoveryService(db).import_candidate(user_id, candidate_id)
                except ValueError as exc:
                    await raw_db.rollback()
                    return str(exc)
                await raw_db.commit()
                return None

        delete_task = asyncio.create_task(delete_account())
        await asyncio.wait_for(delete_locked_account.wait(), timeout=2)
        import_task = asyncio.create_task(import_candidate())
        account_wait = asyncio.create_task(import_attempted_account.wait())
        later_wait = asyncio.create_task(import_attempted_later_lock.wait())
        await asyncio.wait_for(
            asyncio.wait(
                {account_wait, later_wait},
                return_when=asyncio.FIRST_COMPLETED,
            ),
            timeout=2,
        )
        account_was_first = (
            import_attempted_account.is_set()
            and not import_attempted_later_lock.is_set()
        )
        for waiter in (account_wait, later_wait):
            if not waiter.done():
                waiter.cancel()
        await asyncio.gather(account_wait, later_wait, return_exceptions=True)
        allow_delete.set()
        _, import_error = await asyncio.wait_for(
            asyncio.gather(delete_task, import_task), timeout=6
        )
        assert account_was_first is True
        assert import_error == "Remote account not found"

        async with async_session() as db:
            assert await db.get(RemoteAccount, account_id) is None
            assert await db.get(DiscoveryCandidate, candidate_id) is None
            binding = await db.get(UserSubscriptionSource, binding_id)
            assert binding.remote_account_id is None
    finally:
        allow_delete.set()
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
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
                credential_generation=1,
                is_enabled=True,
                auth_status="healthy",
                next_scan_at=now - timedelta(minutes=1),
                scan_interval_hours=24,
            )
            db.add(account)
            await db.commit()
            account_id = account.id

        async def admit_once():
            async with async_session() as db:
                return await admit_due(db, now=now, publisher=publish)

        first, second = await asyncio.gather(admit_once(), admit_once())
        async with async_session() as db:
            third = await admit_due(db, now=now, publisher=publish)
            target_tasks = list(
                (
                    await db.execute(
                        select(TaskRun).where(
                            TaskRun.triggering_remote_account_id == account_id,
                            TaskRun.operation_type == "remote-discovery-scan",
                        )
                    )
                ).scalars()
            )
            assert first["created"] + second["created"] == 1
            assert third["created"] == 0
            assert len(target_tasks) == 1
            assert (str(target_tasks[0].id), "discovery") in published
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_due_discovery_admission_filters_to_effective_preview_sources(monkeypatch):
    """Closed X/Bilibili providers must not be claimed beside an open Pixiv account."""

    from sqlalchemy import select

    from app.config import settings
    from app.database import async_session, engine
    from app.models import RemoteAccount, TaskRun, User
    from app.services.remote_discovery import admit_due_remote_accounts

    monkeypatch.setattr(settings, "remote_discovery_private_members_enabled", True)
    monkeypatch.setattr(settings, "remote_discovery_pixiv_preview_enabled", True)
    monkeypatch.setattr(settings, "remote_discovery_x_enabled", False)
    monkeypatch.setattr(settings, "remote_discovery_bilibili_enabled", False)
    published: list[tuple[str, str]] = []

    def publish(task_id, *, queue_name):
        published.append((str(task_id), queue_name))

    now = datetime.now(timezone.utc)
    due_at = now - timedelta(minutes=1)
    try:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
            user = User(
                username=f"shared_schedule_test_provider_gate_{uuid4().hex[:8]}",
                password_hash="test-only",
                is_active=True,
            )
            db.add(user)
            await db.flush()
            accounts = {
                source: RemoteAccount(
                    user_id=user.id,
                    source=source,
                    auth_method={"pixiv": "refresh_token", "x": "cookie", "bilibili": "sessdata"}[source],
                    credential_ciphertext=f"test-{source}-ciphertext",
                    credential_key_version=1,
                    credential_generation=1,
                    is_enabled=True,
                    auth_status="healthy",
                    next_scan_at=due_at,
                    scan_interval_hours=24,
                )
                for source in ("pixiv", "x", "bilibili")
            }
            db.add_all(accounts.values())
            await db.commit()
            account_ids = {source: account.id for source, account in accounts.items()}

        async with async_session() as db:
            result = await admit_due_remote_accounts(db, now=now, publisher=publish)

        async with async_session() as db:
            stored = {
                account.source: account
                for account in (
                    await db.execute(
                        select(RemoteAccount).where(RemoteAccount.id.in_(account_ids.values()))
                    )
                ).scalars()
            }
            tasks = list(
                (
                    await db.execute(
                        select(TaskRun).where(
                            TaskRun.triggering_remote_account_id.in_(account_ids.values()),
                            TaskRun.operation_type == "remote-discovery-scan",
                        )
                    )
                ).scalars()
            )

        assert result["created"] == 1
        assert result["published"] == 1
        assert len(tasks) == 1
        assert tasks[0].triggering_remote_account_id == account_ids["pixiv"]
        assert published == [(str(tasks[0].id), "discovery")]
        assert stored["pixiv"].last_scan_started_at is not None
        for source in ("x", "bilibili"):
            assert stored[source].last_scan_started_at is None
            assert stored[source].next_scan_at == due_at
    finally:
        async with async_session() as db:
            await _cleanup_shared_test_rows(db)
        await engine.dispose()
