"""Remote account lifecycle, adapter boundaries, and X PKCE state safety."""

from __future__ import annotations

import base64
import json
from uuid import uuid4

import pytest


def _vault():
    from app.services.remote_credentials import CredentialVault

    return CredentialVault(base64.urlsafe_b64encode(b"r" * 32).decode())


class FakeAdapter:
    source = "pixiv"

    def __init__(self):
        self.received = []

    async def validate_account(self, credentials):
        from app.remote_discovery.contract import RemoteCandidateIdentity

        materialized = credentials.materialize()
        self.received.append(materialized)
        assert materialized["refresh_token"] == "top-secret-refresh"
        return RemoteCandidateIdentity(
            source="pixiv",
            source_creator_id="4242",
            profile_url="https://www.pixiv.net/users/4242",
            display_name="Remote Artist",
            username="remote_artist",
        )

    async def list_collections(self, credentials):
        from app.remote_discovery.contract import RemoteCollection

        assert credentials.materialize()["refresh_token"] == "top-secret-refresh"
        return (RemoteCollection("public", "Public", {"restrict": "public"}),)


class FakeRegistry:
    def __init__(self, adapter):
        self.adapter = adapter

    def get(self, source):
        assert source == self.adapter.source
        return self.adapter


async def _cleanup_account_fixture(db, marker: str) -> None:
    from sqlalchemy import text

    params = {"marker": f"{marker}%"}
    for table in (
        "discovery_candidates",
        "user_subscription_sources",
        "remote_accounts",
        "user_subscriptions",
    ):
        await db.execute(
            text(
                f"DELETE FROM {table} WHERE user_id IN "
                "(SELECT id FROM users WHERE username LIKE :marker)"
            ),
            params,
        )
    await db.execute(
        text(
            "DELETE FROM subscription_sources WHERE subscription_id IN "
            "(SELECT s.id FROM subscriptions s JOIN creators c ON c.id=s.creator_id "
            "WHERE c.name LIKE :marker)"
        ),
        params,
    )
    await db.execute(
        text(
            "DELETE FROM subscriptions WHERE creator_id IN "
            "(SELECT id FROM creators WHERE name LIKE :marker)"
        ),
        params,
    )
    await db.execute(
        text("DELETE FROM creators WHERE name LIKE :marker"),
        params,
    )
    await db.execute(
        text("DELETE FROM users WHERE username LIKE :marker"),
        params,
    )
    await db.commit()


async def _seed_bound_account(db, marker: str):
    from datetime import datetime, timedelta, timezone

    from app.models import (
        Creator,
        Subscription,
        SubscriptionSource,
        User,
        UserSubscription,
        UserSubscriptionSource,
    )
    from app.services.remote_accounts import RemoteAccountService
    from app.services.subscription_membership import (
        recompute_subscription_membership_cache,
    )

    user = User(username=marker, password_hash="test-only", is_active=True)
    db.add(user)
    creator = Creator(name=marker)
    db.add(creator)
    await db.flush()
    service = RemoteAccountService(
        db,
        user.id,
        vault=_vault(),
        adapters=FakeRegistry(FakeAdapter()),
    )
    account = await service.create(
        {
            "source": "pixiv",
            "auth_method": "refresh_token",
            "credentials": {"refresh_token": "top-secret-refresh"},
        }
    )
    await service.test(account.id)
    subscription = Subscription(creator_id=creator.id, name=marker)
    db.add(subscription)
    await db.flush()
    source = SubscriptionSource(
        subscription_id=subscription.id,
        source="pixiv",
        source_creator_id="4242",
        source_url="https://www.pixiv.net/users/4242",
    )
    member = UserSubscription(
        user_id=user.id,
        subscription_id=subscription.id,
        name=marker,
    )
    db.add_all([source, member])
    await db.flush()
    binding = UserSubscriptionSource(
        user_id=user.id,
        subscription_id=subscription.id,
        user_subscription_id=member.id,
        subscription_source_id=source.id,
        remote_account_id=account.id,
        is_enabled=True,
        auth_healthy=True,
        auth_status="healthy",
        next_sync_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db.add(binding)
    await db.flush()
    await recompute_subscription_membership_cache(db, subscription.id)
    return service, account, subscription, source, member, binding


async def _force_imported_provenance(db, account, subscription, member):
    from app.models import DiscoveryCandidate

    candidate = DiscoveryCandidate(
        remote_account_id=account.id,
        user_id=account.user_id,
        source_creator_id=f"imported-{uuid4().hex}",
        state="imported",
        subscription_id=subscription.id,
        user_subscription_id=member.id,
    )
    db.add(candidate)
    await db.flush()
    return candidate


@pytest.mark.integration
@pytest.mark.asyncio
async def test_credential_generation_changes_only_with_credential_identity():
    """Credential provenance is monotonic and independent from row timestamps."""

    from app.database import async_session, engine
    from app.models import RemoteAccount

    marker = f"remote_account_generation_{uuid4().hex}"
    try:
        async with async_session() as db:
            service, account_read, subscription, _source, member, _binding = (
                await _seed_bound_account(db, marker)
            )
            account = await db.get(RemoteAccount, account_read.id)
            assert account.credential_generation == 1

            await service.update(
                account.id,
                {"collection_selectors": [{"restrict": "public"}]},
            )
            await service.test(account.id)
            assert account.credential_generation == 1

            await service.update(
                account.id,
                {"credentials": {"refresh_token": "replacement-generation-token"}},
            )
            assert account.credential_generation == 2

            await _force_imported_provenance(db, account, subscription, member)
            await service.delete(account.id)
            assert account.credential_generation == 3
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unrelated_account_update_keeps_matching_job_generation_current():
    """Policy edits must not make an exact credential outcome falsely stale."""

    from datetime import datetime, timezone

    from app.database import async_session, engine
    from app.models import DownloadJob, RemoteAccount, UserSubscriptionSource
    from app.services.subscription_enqueue import mark_source_auth_failure

    marker = f"remote_gen_policy_{uuid4().hex}"
    try:
        async with async_session() as db:
            service, account_read, subscription, source, member, binding = (
                await _seed_bound_account(db, marker)
            )
            account = await db.get(RemoteAccount, account_read.id)
            # Isolate this test from initial-generation behavior: it exercises
            # the exact-match boundary after a noncredential row update.
            account.credential_generation = 7
            job = DownloadJob(
                subscription_id=subscription.id,
                subscription_source_id=source.id,
                triggering_user_subscription_id=member.id,
                triggering_remote_account_id=account.id,
                triggering_credential_generation=7,
                source=source.source,
                source_url=source.source_url,
                status="enqueued",
            )
            db.add(job)
            await db.commit()

            await service.update(
                account.id,
                {"collection_selectors": [{"restrict": "private"}]},
            )
            assert account.credential_generation == 7
            await mark_source_auth_failure(
                db,
                job,
                "HTTP 401 Unauthorized",
                when=datetime.now(timezone.utc),
            )

            stored_binding = await db.get(UserSubscriptionSource, binding.id)
            assert account.auth_status == "unhealthy"
            assert stored_binding.auth_status == "unhealthy"
            await db.delete(job)
            await db.commit()
    finally:
        async with async_session() as db:
            await _cleanup_account_fixture(db, marker)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_success_cannot_revive_tombstoned_account_or_binding():
    """A pre-delete download receipt must leave deleted private provenance quarantined."""

    from datetime import datetime, timezone

    from app.database import async_session, engine
    from app.models import RemoteAccount, UserSubscriptionSource
    from app.services.remote_accounts import RemoteAccountService
    from app.services.subscription_enqueue import mark_source_sync_success

    marker = f"remote_account_stale_success_{uuid4().hex}"
    try:
        async with async_session() as db:
            service, account, subscription, source, member, binding = (
                await _seed_bound_account(db, marker)
            )
            await _force_imported_provenance(db, account, subscription, member)
            account_id = account.id
            source_id = source.id
            binding_id = binding.id
            member_id = member.id
            await db.commit()

            await service.delete(account_id)
            await db.commit()
            deleted_binding = await db.get(UserSubscriptionSource, binding_id)
            deleted_checked_at = deleted_binding.last_auth_checked_at

        async with async_session() as db:
            await mark_source_sync_success(
                db,
                source_id,
                datetime.now(timezone.utc),
                triggering_user_subscription_id=member_id,
                triggering_remote_account_id=account_id,
            )
            await db.commit()

        async with async_session() as db:
            account = await db.get(RemoteAccount, account_id)
            binding = await db.get(UserSubscriptionSource, binding_id)
            service = RemoteAccountService(
                db,
                account.user_id,
                vault=_vault(),
                adapters=FakeRegistry(FakeAdapter()),
            )
            assert account.auth_status == "deleted"
            assert account.is_enabled is False
            assert account.credential_ciphertext is None
            assert binding.auth_status == "deleted"
            assert binding.auth_healthy is False
            assert binding.auth_error_reason == "Remote account deleted"
            assert binding.last_auth_checked_at == deleted_checked_at
            assert binding.last_synced_at is None
            assert binding.next_sync_at is None
            assert await service.list() == []
    finally:
        async with async_session() as db:
            await _cleanup_account_fixture(db, marker)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_auth_failure_cannot_overwrite_tombstone_or_hard_delete():
    """Deleted private provenance is immutable for both tombstone and missing-account jobs."""

    from datetime import datetime, timezone
    from types import SimpleNamespace

    from app.database import async_session, engine
    from app.models import RemoteAccount, UserSubscriptionSource
    from app.services.subscription_enqueue import mark_source_auth_failure

    marker = f"ra_stale_failure_{uuid4().hex[:16]}"
    hard_marker = f"{marker}_hard"
    try:
        async with async_session() as db:
            service, account, subscription, source, member, binding = (
                await _seed_bound_account(db, marker)
            )
            await _force_imported_provenance(db, account, subscription, member)
            tombstone_job = SimpleNamespace(
                subscription_source_id=source.id,
                triggering_user_subscription_id=member.id,
                triggering_remote_account_id=account.id,
                created_at=datetime.now(timezone.utc),
            )
            account_id = account.id
            binding_id = binding.id
            await db.commit()
            await service.delete(account_id)
            await db.commit()
            deleted_binding = await db.get(UserSubscriptionSource, binding_id)
            deleted_checked_at = deleted_binding.last_auth_checked_at

        async with async_session() as db:
            await mark_source_auth_failure(
                db,
                tombstone_job,
                "HTTP 401 Unauthorized",
            )
            await db.commit()
            account = await db.get(RemoteAccount, account_id)
            binding = await db.get(UserSubscriptionSource, binding_id)
            assert account.auth_status == "deleted"
            assert account.is_enabled is False
            assert account.credential_ciphertext is None
            assert binding.auth_status == "deleted"
            assert binding.auth_healthy is False
            assert binding.auth_error_reason == "Remote account deleted"
            assert binding.last_auth_checked_at == deleted_checked_at
            assert binding.next_sync_at is None

        async with async_session() as db:
            hard_service, hard_account, _, hard_source, hard_member, hard_binding = (
                await _seed_bound_account(db, hard_marker)
            )
            hard_job = SimpleNamespace(
                subscription_source_id=hard_source.id,
                triggering_user_subscription_id=hard_member.id,
                triggering_remote_account_id=hard_account.id,
                created_at=datetime.now(timezone.utc),
            )
            hard_account_id = hard_account.id
            hard_binding_id = hard_binding.id
            await db.commit()
            await hard_service.delete(hard_account_id)
            await db.commit()

        async with async_session() as db:
            await mark_source_auth_failure(db, hard_job, "HTTP 403 Forbidden")
            await db.commit()
            assert await db.get(RemoteAccount, hard_account_id) is None
            hard_binding = await db.get(UserSubscriptionSource, hard_binding_id)
            assert hard_binding.remote_account_id is None
            assert hard_binding.auth_status == "deleted"
            assert hard_binding.auth_error_reason == "Remote account deleted"
            assert hard_binding.auth_healthy is False
    finally:
        async with async_session() as db:
            await _cleanup_account_fixture(db, marker)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_pre_reconnect_outcomes_cannot_corrupt_revived_account():
    """An old job ID match is insufficient after the same account ID gets new credentials."""

    from datetime import datetime, timezone
    from types import SimpleNamespace

    from app.database import async_session, engine
    from app.models import RemoteAccount, UserSubscriptionSource
    from app.services.remote_accounts import RemoteAccountService
    from app.services.subscription_enqueue import (
        mark_source_auth_failure,
        mark_source_sync_success,
    )

    marker = f"remote_account_reconnect_race_{uuid4().hex}"
    try:
        async with async_session() as db:
            service, account, subscription, source, member, binding = (
                await _seed_bound_account(db, marker)
            )
            await _force_imported_provenance(db, account, subscription, member)
            stale_job = SimpleNamespace(
                subscription_source_id=source.id,
                triggering_user_subscription_id=member.id,
                triggering_remote_account_id=account.id,
                triggering_credential_generation=1,
                created_at=datetime.now(timezone.utc),
            )
            account_id = account.id
            binding_id = binding.id
            source_id = source.id
            member_id = member.id
            await db.commit()
            await service.delete(account_id)
            await db.commit()

            revived = await service.create(
                {
                    "source": "pixiv",
                    "auth_method": "refresh_token",
                    "credentials": {"refresh_token": "top-secret-refresh"},
                }
            )
            await service.test(revived.id)
            await db.commit()

        async with async_session() as db:
            await mark_source_auth_failure(db, stale_job, "HTTP 401 Unauthorized")
            await mark_source_sync_success(
                db,
                source_id,
                datetime.now(timezone.utc),
                triggering_user_subscription_id=member_id,
                triggering_remote_account_id=account_id,
                triggering_credential_generation=(
                    stale_job.triggering_credential_generation
                ),
            )
            await db.commit()

        async with async_session() as db:
            account = await db.get(RemoteAccount, account_id)
            binding = await db.get(UserSubscriptionSource, binding_id)
            assert account.auth_status == "healthy"
            assert account.is_enabled is True
            assert account.credential_ciphertext is not None
            assert binding.auth_status == "healthy"
            assert binding.auth_healthy is True
            assert binding.auth_error_reason is None
            service = RemoteAccountService(
                db,
                account.user_id,
                vault=_vault(),
                adapters=FakeRegistry(FakeAdapter()),
            )
            assert [item.id for item in await service.list()] == [account_id]
            assert (await service.test(account_id)).auth_status == "healthy"
    finally:
        async with async_session() as db:
            await _cleanup_account_fixture(db, marker)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remote_account_delete_tombstones_imported_provenance_and_create_revives_it():
    """Delete clears secrets while imported provenance survives a later reconnect."""
    from sqlalchemy import select, text

    from app.database import async_session, engine
    from app.models import (
        Creator,
        DiscoveryCandidate,
        RemoteAccount,
        Subscription,
        SubscriptionSource,
        User,
        UserSubscription,
        UserSubscriptionSource,
    )
    from app.services.remote_accounts import RemoteAccountService
    from app.services.subscription_membership import (
        recompute_subscription_membership_cache,
        select_eligible_membership_source,
    )

    marker = f"remote_account_test_{uuid4().hex}"
    adapter = FakeAdapter()
    try:
        async with async_session() as db:
            user = User(
                username=marker,
                password_hash="test-only",
                is_active=True,
                permissions=["subscriptions"],
            )
            other = User(
                username=f"{marker}_other",
                password_hash="test-only",
                is_active=True,
                permissions=["subscriptions"],
            )
            db.add_all([user, other])
            await db.flush()
            service = RemoteAccountService(db, user.id, vault=_vault(), adapters=FakeRegistry(adapter))
            account = await service.create(
                {
                    "source": "pixiv",
                    "auth_method": "refresh_token",
                    "credentials": {"refresh_token": "top-secret-refresh"},
                    "collection_selectors": [{"restrict": "public"}],
                }
            )
            await db.commit()

            stored = await db.get(RemoteAccount, account.id)
            assert stored.credential_ciphertext
            assert "top-secret-refresh" not in stored.credential_ciphertext
            public = await service.get(account.id)
            assert public.has_credentials is True
            assert public.credential_mask == {"refresh_token": "••••"}
            dumped = public.model_dump_json()
            assert "top-secret-refresh" not in dumped
            assert "credential_ciphertext" not in dumped
            with pytest.raises(ValueError, match="Remote account not found"):
                await RemoteAccountService(
                    db, other.id, vault=_vault(), adapters=FakeRegistry(adapter)
                ).get(account.id)

            tested = await service.test(account.id)
            assert tested.auth_status == "healthy"
            assert tested.remote_user_id == "4242"
            assert tested.remote_username == "remote_artist"
            collections = await service.collections(account.id)
            assert [item.id for item in collections] == ["public"]

            creator = Creator(name="Imported Account Artist")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="Imported")
            db.add(subscription)
            await db.flush()
            canonical_source = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id="4242",
                source_url="https://www.pixiv.net/users/4242",
            )
            db.add(canonical_source)
            await db.flush()
            member = UserSubscription(
                user_id=user.id,
                subscription_id=subscription.id,
                name="Imported",
            )
            db.add(member)
            await db.flush()
            binding = UserSubscriptionSource(
                user_id=user.id,
                subscription_id=subscription.id,
                user_subscription_id=member.id,
                subscription_source_id=canonical_source.id,
                remote_account_id=account.id,
                next_sync_at=None,
            )
            pending = DiscoveryCandidate(
                remote_account_id=account.id,
                user_id=user.id,
                source_creator_id="pending",
                state="pending",
            )
            imported = DiscoveryCandidate(
                remote_account_id=account.id,
                user_id=user.id,
                source_creator_id="4242",
                state="imported",
                subscription_id=subscription.id,
                user_subscription_id=member.id,
            )
            db.add_all([binding, pending, imported])
            await recompute_subscription_membership_cache(db, subscription.id)
            assert canonical_source.is_enabled is True
            await db.commit()

            await service.delete(account.id)
            await db.commit()
            tombstone = await db.get(RemoteAccount, account.id)
            assert tombstone is not None
            assert tombstone.auth_status == "deleted"
            assert tombstone.is_enabled is False
            assert tombstone.credential_ciphertext is None
            assert tombstone.credential_metadata is None
            assert tombstone.credential_key_version is None
            assert await db.get(UserSubscription, member.id) is not None
            await db.refresh(binding)
            assert binding.remote_account_id == account.id
            assert binding.is_enabled is True
            assert binding.auth_healthy is False
            assert binding.auth_status == "deleted"
            assert binding.auth_error_reason == "Remote account deleted"
            assert binding.next_sync_at is None
            await db.refresh(canonical_source)
            assert canonical_source.is_enabled is False
            assert canonical_source.auth_healthy is False
            assert canonical_source.next_sync_at is None
            assert await select_eligible_membership_source(
                db,
                canonical_source,
                now=canonical_source.updated_at,
                preferred_membership_id=member.id,
                preferred_account_id=account.id,
                require_due=False,
            ) is None
            candidates = (
                await db.execute(
                    select(DiscoveryCandidate).where(DiscoveryCandidate.user_id == user.id)
                )
            ).scalars().all()
            assert [candidate.id for candidate in candidates] == [imported.id]
            assert candidates[0].user_subscription_id == member.id
            assert await service.list() == []
            with pytest.raises(ValueError, match="Remote account not found"):
                await service.get(account.id)

            revived = await service.create(
                {
                    "source": "pixiv",
                    "auth_method": "refresh_token",
                    "credentials": {"refresh_token": "top-secret-refresh"},
                    "collection_selectors": [{"restrict": "private"}],
                }
            )
            await db.commit()
            assert revived.id == account.id
            assert revived.auth_status == "untested"
            assert revived.has_credentials is True
            assert revived.collection_selectors == [{"restrict": "private"}]
            assert [listed.id for listed in await service.list()] == [account.id]
            assert await db.get(DiscoveryCandidate, imported.id) is not None
            await db.refresh(binding)
            assert binding.remote_account_id == revived.id
            assert binding.auth_healthy is False
            await db.refresh(canonical_source)
            assert canonical_source.is_enabled is False

            validated = await service.test(revived.id)
            assert validated.auth_status == "healthy"
            await db.refresh(binding)
            await db.refresh(canonical_source)
            assert binding.auth_healthy is True
            assert canonical_source.is_enabled is True
            selected = await select_eligible_membership_source(
                db,
                canonical_source,
                now=canonical_source.updated_at,
                preferred_membership_id=member.id,
                preferred_account_id=revived.id,
                require_due=False,
            )
            assert selected is not None
            assert selected.binding.id == binding.id
    finally:
        async with async_session() as db:
            await db.execute(
                text(
                    "DELETE FROM discovery_candidates WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE :marker)"
                ),
                {"marker": f"{marker}%"},
            )
            await db.execute(
                text(
                    "DELETE FROM user_subscription_sources WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE :marker)"
                ),
                {"marker": f"{marker}%"},
            )
            await db.execute(
                text(
                    "DELETE FROM remote_accounts WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE :marker)"
                ),
                {"marker": f"{marker}%"},
            )
            await db.execute(
                text(
                    "DELETE FROM user_subscriptions WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE :marker)"
                ),
                {"marker": f"{marker}%"},
            )
            await db.execute(
                text("DELETE FROM users WHERE username LIKE :marker"),
                {"marker": f"{marker}%"},
            )
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hard_deleted_account_binding_cannot_fall_back_to_legacy_null_auth():
    """Clearing a deleted account FK must not convert private auth into legacy auth."""

    from datetime import datetime, timezone

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
    from app.services.subscription_membership import (
        recompute_subscription_membership_cache,
        select_eligible_membership_source,
    )

    marker = f"remote_account_hard_delete_{uuid4().hex}"
    try:
        async with async_session() as db:
            service, account, _subscription, source, member, binding = (
                await _seed_bound_account(db, marker)
            )
            await db.commit()

            assert source.is_enabled is True
            assert await select_eligible_membership_source(
                db,
                source,
                now=datetime.now(timezone.utc),
                preferred_membership_id=member.id,
                preferred_account_id=account.id,
            ) is not None

            await service.delete(account.id)
            await db.commit()

            assert await db.get(RemoteAccount, account.id) is None
            assert await db.get(UserSubscription, member.id) is not None
            await db.refresh(binding)
            await db.refresh(source)
            assert binding.remote_account_id is None
            assert binding.is_enabled is True
            assert binding.auth_healthy is False
            assert binding.auth_status == "deleted"
            assert binding.auth_error_reason == "Remote account deleted"
            assert binding.next_sync_at is None
            assert source.is_enabled is False
            assert source.auth_healthy is False
            assert source.next_sync_at is None
            assert await service.list() == []
            assert await select_eligible_membership_source(
                db,
                source,
                now=datetime.now(timezone.utc),
                preferred_membership_id=member.id,
                require_due=False,
                require_preferred_account_match=True,
            ) is None
            assert await select_eligible_membership_source(
                db,
                source,
                now=datetime.now(timezone.utc),
                preferred_membership_id=member.id,
            ) is None

            # A migrated binding that was NULL and healthy from inception keeps
            # the intentional global-config compatibility path.
            legacy_marker = f"{marker}_l"
            legacy_user = User(
                username=legacy_marker,
                password_hash="test-only",
                is_active=True,
            )
            legacy_creator = Creator(name=legacy_marker)
            db.add_all([legacy_user, legacy_creator])
            await db.flush()
            legacy_subscription = Subscription(
                creator_id=legacy_creator.id,
                name=legacy_marker,
            )
            db.add(legacy_subscription)
            await db.flush()
            legacy_source = SubscriptionSource(
                subscription_id=legacy_subscription.id,
                source="pixiv",
                source_creator_id="legacy-null",
                source_url="https://www.pixiv.net/users/999999",
            )
            legacy_member = UserSubscription(
                user_id=legacy_user.id,
                subscription_id=legacy_subscription.id,
                name=legacy_marker,
            )
            db.add_all([legacy_source, legacy_member])
            await db.flush()
            legacy_binding = UserSubscriptionSource(
                user_id=legacy_user.id,
                subscription_id=legacy_subscription.id,
                user_subscription_id=legacy_member.id,
                subscription_source_id=legacy_source.id,
                remote_account_id=None,
                is_enabled=True,
                auth_healthy=True,
                auth_status="healthy",
                next_sync_at=None,
            )
            db.add(legacy_binding)
            await db.flush()
            await recompute_subscription_membership_cache(db, legacy_subscription.id)
            selected = await select_eligible_membership_source(
                db,
                legacy_source,
                now=datetime.now(timezone.utc),
                preferred_membership_id=legacy_member.id,
                require_due=False,
                require_preferred_account_match=True,
            )
            assert selected is not None
            assert selected.binding.id == legacy_binding.id
            assert selected.account is None
            await db.rollback()
    finally:
        async with async_session() as db:
            await _cleanup_account_fixture(db, marker)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remote_account_enablement_recomputes_cache_without_healing_binding():
    """Account toggles affect eligibility but preserve private preference and auth."""

    from datetime import datetime, timezone

    from app.database import async_session, engine
    from app.services.subscription_membership import (
        recompute_subscription_membership_cache,
        select_eligible_membership_source,
    )

    marker = f"remote_account_toggle_{uuid4().hex}"
    try:
        async with async_session() as db:
            service, account, subscription, source, member, binding = (
                await _seed_bound_account(db, marker)
            )
            await db.commit()

            await service.update(account.id, {"is_enabled": False})
            assert binding.is_enabled is True
            assert binding.auth_healthy is True
            assert source.is_enabled is False
            assert await select_eligible_membership_source(
                db,
                source,
                now=datetime.now(timezone.utc),
                preferred_membership_id=member.id,
                preferred_account_id=account.id,
                require_due=False,
            ) is None

            await service.update(account.id, {"is_enabled": True})
            assert binding.is_enabled is True
            assert binding.auth_healthy is True
            assert source.is_enabled is True
            assert await select_eligible_membership_source(
                db,
                source,
                now=datetime.now(timezone.utc),
                preferred_membership_id=member.id,
                preferred_account_id=account.id,
                require_due=False,
            ) is not None

            binding.auth_healthy = False
            binding.auth_status = "unhealthy"
            binding.auth_error_reason = "HTTP 401 Unauthorized"
            await recompute_subscription_membership_cache(db, subscription.id)
            assert source.is_enabled is False

            await service.update(account.id, {"is_enabled": False})
            await service.update(account.id, {"is_enabled": True})
            assert binding.is_enabled is True
            assert binding.auth_healthy is False
            assert binding.auth_error_reason == "HTTP 401 Unauthorized"
            assert source.is_enabled is False
            assert await select_eligible_membership_source(
                db,
                source,
                now=datetime.now(timezone.utc),
                preferred_membership_id=member.id,
                preferred_account_id=account.id,
                require_due=False,
            ) is None
            await db.rollback()
    finally:
        async with async_session() as db:
            await _cleanup_account_fixture(db, marker)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remote_account_policies_are_provider_specific_and_rejected_atomically():
    """Only provider-emitted selectors and non-secret scope values are persisted."""
    from sqlalchemy import text

    from app.database import async_session, engine
    from app.models import RemoteAccount, User
    from app.services.remote_accounts import RemoteAccountService

    marker = f"ra_policy_{uuid4().hex[:20]}"
    try:
        async with async_session() as db:
            user = User(
                username=marker,
                password_hash="test-only",
                is_active=True,
                permissions=["subscriptions"],
            )
            attacker = User(
                username=f"{marker}_attacker",
                password_hash="test-only",
                is_active=True,
                permissions=["subscriptions"],
            )
            db.add_all([user, attacker])
            await db.flush()
            attacker_id = attacker.id
            service = RemoteAccountService(db, user.id, vault=_vault())

            pixiv = await service.create(
                {
                    "source": "pixiv",
                    "auth_method": "refresh_token",
                    "credentials": {"refresh_token": "pixiv-secret"},
                    "scopes": [],
                    "collection_selectors": [
                        {"restrict": "public"},
                        {"restrict": "private"},
                    ],
                }
            )
            x = await service.create(
                {
                    "source": "x",
                    "auth_method": "cookie",
                    "credentials": {"cookie": "x-cookie"},
                    "scopes": ["users.read", "follows.read", "list.read"],
                    "collection_selectors": [
                        {"kind": "following"},
                        {"kind": "list", "list_id": "7719", "private": True},
                    ],
                }
            )
            bilibili = await service.create(
                {
                    "source": "bilibili",
                    "auth_method": "sessdata",
                    "credentials": {"SESSDATA": "bili-cookie"},
                    "scopes": [],
                    "collection_selectors": [
                        {"kind": "all"},
                        {"kind": "group", "group_id": "12", "count": 38},
                    ],
                }
            )
            await db.commit()
            assert pixiv.collection_selectors[1] == {"restrict": "private"}
            assert x.collection_selectors[1]["private"] is True
            assert bilibili.collection_selectors[1]["count"] == 38

            invalid_updates = (
                (pixiv.id, {"scopes": ["users.read"]}),
                (x.id, {"scopes": ["users.read", "tweet.read"]}),
                (
                    x.id,
                    {
                        "collection_selectors": [
                            {
                                "kind": "list",
                                "list_id": {"access_token": "nested-secret"},
                            }
                        ]
                    },
                ),
                (
                    bilibili.id,
                    {
                        "collection_selectors": [
                            {"kind": "group", "group_id": "12", "SESSDATA": "secret"}
                        ]
                    },
                ),
                (
                    pixiv.id,
                    {
                        "collection_selectors": [
                            {"restrict": "public", "cookie": "session=secret"}
                        ]
                    },
                ),
                (
                    pixiv.id,
                    {"collection_selectors": [{"restrict": "public"}] * 201},
                ),
            )
            for account_id, payload in invalid_updates:
                with pytest.raises(ValueError):
                    await service.update(account_id, payload)

            db.expire_all()
            stored_pixiv = await db.get(RemoteAccount, pixiv.id)
            stored_x = await db.get(RemoteAccount, x.id)
            stored_bilibili = await db.get(RemoteAccount, bilibili.id)
            assert stored_pixiv.scopes == []
            assert stored_pixiv.collection_selectors == [
                {"restrict": "public"},
                {"restrict": "private"},
            ]
            assert stored_x.scopes == ["users.read", "follows.read", "list.read"]
            assert stored_bilibili.collection_selectors[1] == {
                "kind": "group",
                "group_id": "12",
                "count": 38,
            }

            attacker_service = RemoteAccountService(db, attacker_id, vault=_vault())
            with pytest.raises(ValueError):
                await attacker_service.create(
                    {
                        "source": "x",
                        "auth_method": "cookie",
                        "credentials": {"cookie": "x-cookie"},
                        "scopes": ["offline.access", "authorization=Bearer nested-secret"],
                        "collection_selectors": [{"kind": "following"}],
                    }
                )
    finally:
        async with async_session() as db:
            await db.execute(
                text(
                    "DELETE FROM remote_accounts WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE :marker)"
                ),
                {"marker": f"{marker}%"},
            )
            await db.execute(
                text("DELETE FROM users WHERE username LIKE :marker"),
                {"marker": f"{marker}%"},
            )
            await db.commit()
        await engine.dispose()


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def setex(self, key, ttl, value):
        self.values[key] = value
        self.ttls[key] = ttl

    def getdel(self, key):
        return self.values.pop(key, None)

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        return int(self.values.pop(key, None) is not None)

    def ttl(self, key):
        return self.ttls.get(key, -2)


class FakeOAuthExchange:
    def __init__(self):
        self.calls = []

    async def exchange(self, *, code, verifier, redirect_uri, client_id):
        self.calls.append((code, verifier, redirect_uri, client_id))
        return {
            "access_token": "oauth-access-secret",
            "refresh_token": "oauth-refresh-secret",
            "scope": "users.read follows.read list.read offline.access",
        }


def test_x_pkce_state_is_ten_minute_single_use_and_owner_bound():
    """Redis state contains no tokens and replay/cross-owner callbacks are rejected."""
    from app.services.x_oauth import XOAuthPKCEState

    redis = MemoryRedis()
    state_service = XOAuthPKCEState(
        redis,
        client_id="public-client",
        redirect_uri="https://gallery.example/api/v1/remote-accounts/x/oauth/callback",
    )
    authorization = state_service.authorize(user_id=7)
    assert authorization.state
    assert "code_challenge=" in authorization.url
    assert "users.read" in authorization.url
    key = f"remote-discovery:x:oauth-state:{authorization.state}"
    assert redis.ttls[key] == 600
    stored = redis.values[key].decode() if isinstance(redis.values[key], bytes) else redis.values[key]
    assert "token" not in stored.casefold()

    with pytest.raises(ValueError, match="owner"):
        redis.ttls[key] = 321
        state_service.consume(state=authorization.state, user_id=8)
    # An ownership failure must not burn the rightful user's state.
    assert redis.ttls[key] == 321
    payload = state_service.consume(state=authorization.state, user_id=7)
    assert payload.verifier
    with pytest.raises(ValueError, match="expired or already used"):
        state_service.consume(state=authorization.state, user_id=7)


def test_remote_account_write_schemas_accept_credentials_but_reads_cannot_serialize_them():
    from pydantic import ValidationError

    from app.schemas.remote_discovery import RemoteAccountCreate, RemoteAccountRead

    created = RemoteAccountCreate(
        source="bilibili",
        auth_method="sessdata",
        credentials={"SESSDATA": "secret-cookie"},
    )
    assert created.credentials == {"SESSDATA": "secret-cookie"}
    assert "credentials" not in RemoteAccountRead.model_fields
    assert "credential_ciphertext" not in RemoteAccountRead.model_fields
    assert {"has_credentials", "credential_mask"} <= set(RemoteAccountRead.model_fields)
    with pytest.raises(ValidationError) as invalid:
        RemoteAccountCreate(
            source="pixiv",
            auth_method="cookie",
            credentials={"cookie": "must-not-echo-this-secret"},
        )
    assert "must-not-echo-this-secret" not in str(invalid.value)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remote_account_auth_method_cannot_relabel_existing_ciphertext():
    """Auth method changes require compatible replacement credentials."""
    from sqlalchemy import text

    from app.database import async_session, engine
    from app.models import User
    from app.services.remote_accounts import RemoteAccountService

    marker = f"remote_account_method_{uuid4().hex}"
    try:
        async with async_session() as db:
            user = User(username=marker, password_hash="x", is_active=True)
            db.add(user)
            await db.flush()
            service = RemoteAccountService(db, user.id, vault=_vault(), adapters=FakeRegistry(FakeAdapter()))
            account = await service.create(
                {
                    "source": "x",
                    "auth_method": "oauth2",
                    "credentials": {"access_token": "top-secret-access"},
                }
            )
            with pytest.raises(ValueError, match="replacement credentials"):
                await service.update(account.id, {"auth_method": "cookie"})
            with pytest.raises(ValueError, match="not valid"):
                await service.update(
                    account.id,
                    {"auth_method": "sessdata", "credentials": {"SESSDATA": "secret"}},
                )
            with pytest.raises(ValueError, match="unexpected credential fields"):
                await service.update(
                    account.id,
                    {
                        "credentials": {
                            "access_token": "replacement-access",
                            "a-secret-value-used-as-a-key": "ignored",
                        }
                    },
                )
    finally:
        async with async_session() as db:
            await db.execute(
                text(
                    "DELETE FROM remote_accounts WHERE user_id IN "
                    "(SELECT id FROM users WHERE username=:marker)"
                ),
                {"marker": marker},
            )
            await db.execute(text("DELETE FROM users WHERE username=:marker"), {"marker": marker})
            await db.commit()
        await engine.dispose()


def test_x_oauth_scope_validation_requires_every_discovery_scope():
    from app.services.x_oauth import validate_x_oauth_scopes

    assert validate_x_oauth_scopes(
        "users.read follows.read list.read offline.access"
    ) == ["users.read", "follows.read", "list.read", "offline.access"]
    with pytest.raises(ValueError, match="missing required scopes"):
        validate_x_oauth_scopes("users.read follows.read")


def _headers(username: str) -> dict[str, str]:
    from app.auth import create_access_token

    return {
        "Authorization": f"Bearer {create_access_token(username, must_change_password=False)}"
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remote_account_api_is_owner_scoped_redacted_and_admin_audit_is_metadata_only():
    """Own-account APIs and administrator audit must never serialize credential material."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text

    from app.config import settings
    from app.database import async_session, engine
    from app.main import app
    from app.models import User
    from app.remote_discovery import registry

    marker = f"remote_account_api_{uuid4().hex}"
    old_key = settings.remote_credential_key
    old_adapter = registry._adapters.get("pixiv")
    settings.remote_credential_key = base64.urlsafe_b64encode(b"a" * 32).decode()
    registry.register(FakeAdapter())
    try:
        async with async_session() as db:
            first = User(
                username=marker,
                password_hash="test-only",
                is_active=True,
                permissions=["subscriptions"],
            )
            second = User(
                username=f"{marker}_second",
                password_hash="test-only",
                is_active=True,
                permissions=["subscriptions"],
            )
            admin = User(
                username=f"{marker}_admin",
                password_hash="test-only",
                is_active=True,
                is_admin=True,
            )
            db.add_all([first, second, admin])
            await db.commit()
            first_name, second_name, admin_name = first.username, second.username, admin.username

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/remote-accounts",
                json={
                    "source": "pixiv",
                    "auth_method": "refresh_token",
                    "credentials": {"refresh_token": "top-secret-refresh"},
                },
                headers=_headers(first_name),
            )
            assert response.status_code == 201, response.text
            body = response.json()
            account_id = body["id"]
            assert body["has_credentials"] is True
            assert body["credential_mask"] == {"refresh_token": "••••"}
            assert "top-secret-refresh" not in response.text
            assert "ciphertext" not in response.text

            assert (
                await client.get(f"/api/v1/remote-accounts/{account_id}", headers=_headers(second_name))
            ).status_code == 404
            tested = await client.post(
                f"/api/v1/remote-accounts/{account_id}/test", headers=_headers(first_name)
            )
            assert tested.status_code == 200, tested.text
            assert tested.json()["auth_status"] == "healthy"
            collections = await client.get(
                f"/api/v1/remote-accounts/{account_id}/collections", headers=_headers(first_name)
            )
            assert collections.status_code == 200
            assert collections.json() == [
                {"id": "public", "name": "Public", "selector": {"restrict": "public"}}
            ]

            forbidden = await client.get(
                "/api/v1/admin/audit/remote-accounts", headers=_headers(second_name)
            )
            assert forbidden.status_code == 403
            audit = await client.get(
                "/api/v1/admin/audit/remote-accounts", headers=_headers(admin_name)
            )
            assert audit.status_code == 200, audit.text
            assert audit.json()["total"] == 1
            item = audit.json()["items"][0]
            assert item["has_credentials"] is True
            assert "credential_mask" not in item
            assert "credential" not in json.dumps(item).replace("has_credentials", "")
            assert "top-secret-refresh" not in audit.text
    finally:
        settings.remote_credential_key = old_key
        if old_adapter is not None:
            registry.register(old_adapter)
        async with async_session() as db:
            await db.execute(
                text(
                    "DELETE FROM discovery_candidates WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE :marker)"
                ),
                {"marker": f"{marker}%"},
            )
            await db.execute(
                text(
                    "DELETE FROM user_subscription_sources WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE :marker)"
                ),
                {"marker": f"{marker}%"},
            )
            await db.execute(
                text(
                    "DELETE FROM remote_accounts WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE :marker)"
                ),
                {"marker": f"{marker}%"},
            )
            await db.execute(
                text(
                    "DELETE FROM user_subscriptions WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE :marker)"
                ),
                {"marker": f"{marker}%"},
            )
            await db.execute(
                text("DELETE FROM users WHERE username LIKE :marker"),
                {"marker": f"{marker}%"},
            )
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_x_oauth_api_uses_injected_exchange_and_rejects_replay():
    """OAuth callback stores encrypted tokens only after consuming owner-bound PKCE state."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select, text

    from app.config import settings
    from app.database import async_session, engine
    from app.main import app
    from app.models import RemoteAccount, User
    from app.services.redis_client import get_redis
    from app.services.x_oauth import get_x_oauth_exchange

    marker = f"x_oauth_api_{uuid4().hex}"
    redis = MemoryRedis()
    exchange = FakeOAuthExchange()
    old_key = settings.remote_credential_key
    old_client_id = settings.x_oauth_client_id
    old_redirect = settings.x_oauth_redirect_uri
    settings.remote_credential_key = base64.urlsafe_b64encode(b"o" * 32).decode()
    settings.x_oauth_client_id = "test-public-client"
    settings.x_oauth_redirect_uri = "https://gallery.example/api/v1/remote-accounts/x/oauth/callback"
    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[get_x_oauth_exchange] = lambda: exchange
    try:
        async with async_session() as db:
            user = User(
                username=marker,
                password_hash="test-only",
                is_active=True,
                permissions=["subscriptions"],
            )
            db.add(user)
            await db.commit()
            username = user.username
            user_id = user.id

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            missing_account = await client.get(
                "/api/v1/remote-accounts/x/oauth/authorize",
                params={"account_id": str(uuid4())},
                headers=_headers(username),
            )
            assert missing_account.status_code == 404
            authorize = await client.get(
                "/api/v1/remote-accounts/x/oauth/authorize", headers=_headers(username)
            )
            assert authorize.status_code == 200, authorize.text
            state = authorize.json()["state"]
            callback = await client.get(
                "/api/v1/remote-accounts/x/oauth/callback",
                params={"state": state, "code": "authorization-code"},
                headers=_headers(username),
            )
            assert callback.status_code == 200, callback.text
            assert callback.json()["source"] == "x"
            assert callback.json()["has_credentials"] is True
            assert "oauth-access-secret" not in callback.text
            replay = await client.get(
                "/api/v1/remote-accounts/x/oauth/callback",
                params={"state": state, "code": "authorization-code"},
                headers=_headers(username),
            )
            assert replay.status_code == 400

        async with async_session() as db:
            account = (
                await db.execute(
                    select(RemoteAccount).where(RemoteAccount.user_id == user_id)
                )
            ).scalar_one()
            assert "oauth-access-secret" not in account.credential_ciphertext
            assert not any("oauth-access-secret" in str(value) for value in redis.values.values())
    finally:
        app.dependency_overrides.pop(get_redis, None)
        app.dependency_overrides.pop(get_x_oauth_exchange, None)
        settings.remote_credential_key = old_key
        settings.x_oauth_client_id = old_client_id
        settings.x_oauth_redirect_uri = old_redirect
        async with async_session() as db:
            await db.execute(
                text(
                    "DELETE FROM remote_accounts WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE :marker)"
                ),
                {"marker": f"{marker}%"},
            )
            await db.execute(
                text("DELETE FROM users WHERE username LIKE :marker"),
                {"marker": f"{marker}%"},
            )
            await db.commit()
        await engine.dispose()
