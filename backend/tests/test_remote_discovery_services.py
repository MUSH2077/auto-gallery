"""Persistent discovery scans, classification, and idempotent candidate imports."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text


PREFIX = "discovery_service_test_"


def _vault():
    from app.services.remote_credentials import CredentialVault

    return CredentialVault(base64.urlsafe_b64encode(b"d" * 32).decode())


async def _seed_user(db, suffix: str):
    from app.models import User

    user = User(
        username=f"{PREFIX}{suffix}_{uuid4().hex[:8]}",
        password_hash="test-only",
        is_active=True,
        permissions=["subscriptions", "tasks"],
    )
    db.add(user)
    await db.flush()
    return user


async def _cleanup(db):
    params = {"prefix": f"{PREFIX}%", "creator": f"{PREFIX}%"}
    await db.execute(
        text(
            "DELETE FROM task_events WHERE task_run_id IN ("
            "SELECT tr.id FROM task_runs tr LEFT JOIN remote_accounts ra "
            "ON ra.id=tr.triggering_remote_account_id LEFT JOIN user_subscriptions us "
            "ON us.id=tr.triggering_user_subscription_id "
            "WHERE ra.user_id IN (SELECT id FROM users WHERE username LIKE :prefix) "
            "OR us.user_id IN (SELECT id FROM users WHERE username LIKE :prefix))"
        ),
        params,
    )
    await db.execute(
        text(
            "DELETE FROM task_runs WHERE triggering_remote_account_id IN ("
            "SELECT id FROM remote_accounts WHERE user_id IN "
            "(SELECT id FROM users WHERE username LIKE :prefix)) "
            "OR triggering_user_subscription_id IN (SELECT id FROM user_subscriptions WHERE user_id IN "
            "(SELECT id FROM users WHERE username LIKE :prefix))"
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
            "DELETE FROM creator_links WHERE creator_id IN "
            "(SELECT id FROM creators WHERE name LIKE :creator)"
        ),
        params,
    )
    await db.execute(
        text(
            "DELETE FROM subscription_sources WHERE subscription_id IN ("
            "SELECT s.id FROM subscriptions s JOIN creators c ON c.id=s.creator_id "
            "WHERE c.name LIKE :creator)"
        ),
        params,
    )
    await db.execute(
        text(
            "DELETE FROM subscriptions WHERE creator_id IN "
            "(SELECT id FROM creators WHERE name LIKE :creator)"
        ),
        params,
    )
    await db.execute(
        text(
            "DELETE FROM source_creators WHERE creator_id IN "
            "(SELECT id FROM creators WHERE name LIKE :creator)"
        ),
        params,
    )
    await db.execute(text("DELETE FROM creators WHERE name LIKE :creator"), params)
    await db.execute(text("DELETE FROM users WHERE username LIKE :prefix"), params)
    await db.commit()


class Registry:
    def __init__(self, adapter):
        self.adapter = adapter

    def get(self, source):
        assert source == self.adapter.source
        return self.adapter


class PagedPixivAdapter:
    source = "pixiv"

    def __init__(self, *, fail_after_first: bool = False):
        self.fail_after_first = fail_after_first
        self.calls = []

    async def fetch_page(self, credentials, *, selector=None, cursor=None, page_size=100):
        from app.remote_discovery.contract import DiscoveryPage, RemoteCandidateIdentity

        values = credentials.materialize()
        assert values["refresh_token"] == "scan-secret"
        self.calls.append((dict(selector or {}), dict(cursor or {})))
        if cursor and self.fail_after_first:
            raise RuntimeError("provider interrupted")
        if not cursor:
            return DiscoveryPage(
                items=[
                    RemoteCandidateIdentity(
                        source="pixiv",
                        source_creator_id="scan-a",
                        profile_url="https://www.pixiv.net/users/1001",
                        display_name=f"{PREFIX}Scan A",
                        metadata={"has_illustration_preview": True},
                    )
                ],
                done=False,
                next_cursor={"offset": 1},
            )
        return DiscoveryPage(
            items=[
                RemoteCandidateIdentity(
                    source="pixiv",
                    source_creator_id="scan-b",
                    profile_url="https://www.pixiv.net/users/1002",
                    display_name=f"{PREFIX}Scan B",
                )
            ],
            done=True,
        )


async def _account(db, user, adapter, *, auto=False, limit=25):
    from app.services.remote_accounts import RemoteAccountService

    service = RemoteAccountService(db, user.id, vault=_vault(), adapters=Registry(adapter))
    account = await service.create(
        {
            "source": "pixiv",
            "auth_method": "refresh_token",
            "credentials": {"refresh_token": "scan-secret"},
            "auto_import_enabled": auto,
            "auto_import_min_confidence": "high",
            "auto_import_limit": limit,
            "collection_selectors": [{"restrict": "public"}],
        }
    )
    return account


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scan_singleflight_pages_are_resumable_and_only_complete_marks_unfollowed():
    """Each page checkpoints atomically; absence is authoritative only after full success."""
    from app.database import async_session, engine
    from app.models import DiscoveryCandidate, RemoteAccount, TaskRun
    from app.services.remote_discovery import DiscoveryScanInProgress, RemoteDiscoveryService

    adapter = PagedPixivAdapter()
    try:
        async with async_session() as db:
            await _cleanup(db)
            user = await _seed_user(db, "scan")
            account = await _account(db, user, adapter)
            user_id = user.id
            account_id = account.id
            old = DiscoveryCandidate(
                remote_account_id=account.id,
                user_id=user.id,
                source_creator_id="no-longer-followed",
                state="pending",
                is_following=True,
                last_seen_at=datetime.now(timezone.utc) - timedelta(days=2),
            )
            db.add(old)
            await db.commit()

            service = RemoteDiscoveryService(db, vault=_vault(), adapters=Registry(adapter))
            task = await service.create_scan(user.id, account.id)
            await db.commit()
            task_id = task.id
            with pytest.raises(DiscoveryScanInProgress):
                await service.create_scan(user.id, account.id)
            await db.rollback()

            completed = await service.run_scan(task_id)
            assert completed.status == "complete"
            assert completed.progress_data["cursor"] is None
            assert completed.progress_data["pages_completed"] == 2
            assert "scan-secret" not in str(completed.progress_data)
            await db.refresh(old)
            assert old.is_following is False
            current = (
                await db.execute(
                    select(DiscoveryCandidate).where(
                        DiscoveryCandidate.remote_account_id == account.id,
                        DiscoveryCandidate.source_creator_id.in_(["scan-a", "scan-b"]),
                    )
                )
            ).scalars().all()
            assert len(current) == 2
            assert all(item.is_following for item in current)
            assert {item.confidence for item in current} == {"high", "low"}
            stored_account = await db.get(RemoteAccount, account.id)
            assert stored_account.scan_cursor is None
            assert stored_account.last_scan_completed_at is not None

            # A second complete scan updates the same rows instead of duplicating.
            next_task = await service.create_scan(user_id, account_id)
            await db.commit()
            await service.run_scan(next_task.id)
            count = (
                await db.execute(
                    select(func.count(DiscoveryCandidate.id)).where(
                        DiscoveryCandidate.remote_account_id == account.id
                    )
                )
            ).scalar_one()
            assert count == 3
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_incomplete_scan_preserves_prior_follow_state_and_checkpoint_without_secret():
    """A later-page failure must retain the cursor and never apply unfollow transitions."""
    from app.database import async_session, engine
    from app.models import DiscoveryCandidate, TaskRun
    from app.services.remote_discovery import RemoteDiscoveryService

    adapter = PagedPixivAdapter(fail_after_first=True)
    try:
        async with async_session() as db:
            await _cleanup(db)
            user = await _seed_user(db, "incomplete")
            account = await _account(db, user, adapter)
            old = DiscoveryCandidate(
                remote_account_id=account.id,
                user_id=user.id,
                source_creator_id="unseen-during-failure",
                state="imported",
                is_following=True,
                last_seen_at=datetime.now(timezone.utc) - timedelta(days=2),
            )
            db.add(old)
            await db.commit()
            service = RemoteDiscoveryService(db, vault=_vault(), adapters=Registry(adapter))
            task = await service.create_scan(user.id, account.id)
            await db.commit()
            with pytest.raises(RuntimeError, match="provider interrupted"):
                await service.run_scan(task.id)

            stored_task = await db.get(TaskRun, task.id)
            assert stored_task.status == "failed"
            assert stored_task.progress_data["cursor"] == {"offset": 1}
            assert "scan-secret" not in str(stored_task.progress_data)
            await db.refresh(old)
            assert old.is_following is True
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_scan_atomically_claims_single_provider_execution_and_requires_stale_requeue():
    """Concurrent workers cannot both cross the adapter boundary for one scan."""
    import asyncio

    from app.database import async_session, engine
    from app.models import TaskRun
    from app.remote_discovery.contract import DiscoveryPage, RemoteCandidateIdentity
    from app.services.remote_discovery import DiscoveryScanInProgress, RemoteDiscoveryService

    class BlockingAdapter(PagedPixivAdapter):
        def __init__(self):
            super().__init__()
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def fetch_page(self, credentials, *, selector, cursor=None, page_size=100):
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return DiscoveryPage(
                items=(
                    RemoteCandidateIdentity(
                        source="pixiv",
                        source_creator_id="single-flight-candidate",
                        profile_url="https://www.pixiv.net/users/7711",
                        display_name="Single Flight",
                        metadata={"has_illustration_preview": True},
                    ),
                ),
                next_cursor=None,
                done=True,
            )

    adapter = BlockingAdapter()
    adapters = Registry(adapter)
    try:
        async with async_session() as db:
            await _cleanup(db)
            user = await _seed_user(db, "singleflight")
            account = await _account(db, user, adapter)
            task = await RemoteDiscoveryService(
                db, vault=_vault(), adapters=adapters
            ).create_scan(user.id, account.id)
            await db.commit()
            task_id = task.id

        async def execute_scan():
            async with async_session() as worker_db:
                return await RemoteDiscoveryService(
                    worker_db, vault=_vault(), adapters=adapters
                ).run_scan(task_id)

        first_run = asyncio.create_task(execute_scan())
        await asyncio.wait_for(adapter.started.wait(), timeout=5)
        with pytest.raises(DiscoveryScanInProgress):
            await asyncio.wait_for(execute_scan(), timeout=2)
        assert adapter.calls == 1
        adapter.release.set()
        completed = await asyncio.wait_for(first_run, timeout=10)
        assert completed.status == "complete"

        # Complete is idempotent and does not cross the provider boundary.
        completed_again = await execute_scan()
        assert completed_again.status == "complete"
        assert adapter.calls == 1

        async with async_session() as db:
            service = RemoteDiscoveryService(db, vault=_vault(), adapters=adapters)
            account_id = (await db.get(TaskRun, task_id)).triggering_remote_account_id
            stale_task = await service.create_scan(user.id, account_id)
            stale_task.status = "stale"
            await db.commit()
            stale_task_id = stale_task.id

        async with async_session() as worker_db:
            with pytest.raises(ValueError, match="re-enqueued"):
                await RemoteDiscoveryService(
                    worker_db, vault=_vault(), adapters=adapters
                ).run_scan(stale_task_id)
            await worker_db.rollback()
            stale_task = await worker_db.get(TaskRun, stale_task_id)
            stale_task.status = "enqueued"
            await worker_db.commit()
            retried = await RemoteDiscoveryService(
                worker_db, vault=_vault(), adapters=adapters
            ).run_scan(stale_task_id)
            assert retried.status == "complete"
        assert adapter.calls == 2
    finally:
        adapter.release.set()
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_automatic_import_is_opt_in_thresholded_capped_and_reported():
    """A complete scan imports only eligible pending rows up to the account cap."""
    from app.database import async_session, engine
    from app.models import DiscoveryCandidate
    from app.services.remote_discovery import RemoteDiscoveryService

    adapter = PagedPixivAdapter()
    try:
        async with async_session() as db:
            await _cleanup(db)
            user = await _seed_user(db, "auto")
            account = await _account(db, user, adapter, auto=True, limit=1)
            service = RemoteDiscoveryService(db, vault=_vault(), adapters=Registry(adapter))
            task = await service.create_scan(user.id, account.id)
            await db.commit()
            completed = await service.run_scan(task.id)
            assert completed.result_data["auto_imported_count"] == 1
            candidates = (
                await db.execute(
                    select(DiscoveryCandidate).where(
                        DiscoveryCandidate.remote_account_id == account.id
                    )
                )
            ).scalars().all()
            states = {candidate.source_creator_id: candidate.state for candidate in candidates}
            assert states == {"scan-a": "imported", "scan-b": "pending"}
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_candidate_state_machine_conflict_and_import_reuse_shared_canonical_rows():
    """Dismiss/restore and conflict resolution remain private; imports reuse global identity."""
    from app.database import async_session, engine
    from app.models import (
        Creator,
        CreatorLink,
        DiscoveryCandidate,
        RemoteAccount,
        SourceCreator,
        Subscription,
        SubscriptionSource,
        UserSubscription,
        UserSubscriptionSource,
    )
    from app.remote_discovery.contract import RemoteCandidateIdentity
    from app.services.remote_discovery import RemoteDiscoveryService
    from app.services.subscription_membership import SubscriptionMembershipService

    adapter = PagedPixivAdapter()
    try:
        async with async_session() as db:
            await _cleanup(db)
            first = await _seed_user(db, "import_first")
            second = await _seed_user(db, "import_second")
            account = await _account(db, first, adapter)
            existing_creator = Creator(name=f"{PREFIX}existing")
            conflicting_creator = Creator(name=f"{PREFIX}conflicting")
            db.add_all([existing_creator, conflicting_creator])
            await db.flush()
            identity = SourceCreator(
                creator_id=existing_creator.id,
                source="pixiv",
                source_creator_id="shared-identity",
                source_url="https://www.pixiv.net/users/7001",
                display_name="Shared Identity",
            )
            db.add(identity)
            await db.flush()
            canonical = Subscription(creator_id=existing_creator.id, name="Canonical Shared")
            db.add(canonical)
            await db.flush()
            canonical_source = SubscriptionSource(
                subscription_id=canonical.id,
                source="pixiv",
                source_creator_id="shared-identity",
                source_url=None,
            )
            db.add(canonical_source)
            await db.flush()
            first_member = await SubscriptionMembershipService(db, first.id).ensure_membership(
                canonical, name="First Existing Private"
            )
            first_binding = await SubscriptionMembershipService(
                db, first.id
            ).ensure_source_binding(first_member, canonical_source)
            second_member = await SubscriptionMembershipService(db, second.id).ensure_membership(
                canonical, name="Second Private"
            )
            db.add(
                CreatorLink(
                    creator_id=conflicting_creator.id,
                    url="https://portfolio.example/shared",
                    link_type="portfolio",
                    source="web",
                    is_verified=True,
                )
            )
            await db.commit()

            service = RemoteDiscoveryService(db, vault=_vault(), adapters=Registry(adapter))
            stored_account = await db.get(RemoteAccount, account.id)
            unique = await service.upsert_candidate(
                stored_account,
                RemoteCandidateIdentity(
                    source="pixiv",
                    source_creator_id="shared-identity",
                    profile_url="https://www.pixiv.net/users/7001",
                    display_name="Shared Identity",
                ),
                seen_at=datetime.now(timezone.utc),
            )
            conflict = await service.upsert_candidate(
                stored_account,
                RemoteCandidateIdentity(
                    source="pixiv",
                    source_creator_id="conflict-identity",
                    profile_url="https://www.pixiv.net/users/7002",
                    display_name="Conflict",
                    metadata={"supported_links": ["https://portfolio.example/shared"]},
                ),
                seen_at=datetime.now(timezone.utc),
                additional_creator_ids={existing_creator.id},
            )
            assert unique.confidence == "high"
            assert conflict.state == "conflict"

            await service.batch_action(first.id, [unique.id], action="dismiss")
            await db.commit()
            assert (await db.get(DiscoveryCandidate, unique.id)).state == "dismissed"
            # Rescan updates snapshot but preserves dismissal.
            await service.upsert_candidate(
                stored_account,
                RemoteCandidateIdentity(
                    source="pixiv",
                    source_creator_id="shared-identity",
                    profile_url="https://www.pixiv.net/users/7001",
                    display_name="Shared Identity Updated",
                ),
                seen_at=datetime.now(timezone.utc),
            )
            assert (await db.get(DiscoveryCandidate, unique.id)).state == "dismissed"
            await service.batch_action(first.id, [unique.id], action="restore")
            imported = await service.batch_action(first.id, [unique.id], action="import")
            await db.commit()
            assert imported[0].state == "imported"
            assert imported[0].subscription_id == canonical.id
            assert imported[0].user_subscription_id != second_member.id
            await db.refresh(first_binding)
            assert first_binding.remote_account_id == account.id
            assert (
                await db.execute(
                    select(func.count(Subscription.id)).where(
                        Subscription.creator_id == existing_creator.id
                    )
                )
            ).scalar_one() == 1
            assert (
                await db.execute(
                    select(func.count(UserSubscription.id)).where(
                        UserSubscription.subscription_id == canonical.id
                    )
                )
            ).scalar_one() == 2
            # Re-import is a no-op and conflicts cannot auto-import.
            repeated = await service.batch_action(first.id, [unique.id], action="import")
            assert repeated[0].user_subscription_id == imported[0].user_subscription_id
            with pytest.raises(ValueError, match="conflict"):
                await service.import_candidate(first.id, conflict.id)

            # A missing remote URL must not alias an unrelated NULL-url source.
            no_url = await service.upsert_candidate(
                stored_account,
                RemoteCandidateIdentity(
                    source="pixiv",
                    source_creator_id="no-url-identity",
                    profile_url=None,
                    display_name="No URL Identity",
                ),
                seen_at=datetime.now(timezone.utc),
                additional_creator_ids={existing_creator.id},
            )
            await service.import_candidate(first.id, no_url.id)
            source_ids = set(
                (
                    await db.execute(
                        select(SubscriptionSource.source_creator_id).where(
                            SubscriptionSource.subscription_id == canonical.id
                        )
                    )
                ).scalars()
            )
            assert source_ids == {"shared-identity", "no-url-identity"}
            bindings = (
                await db.execute(
                    select(func.count(UserSubscriptionSource.id)).where(
                        UserSubscriptionSource.user_subscription_id == first_member.id
                    )
                )
            ).scalar_one()
            assert bindings == 2

            # Re-import repairs a membership that was locally removed while
            # retaining the shared creator/subscription/source rows.
            original_membership_id = imported[0].user_subscription_id
            await SubscriptionMembershipService(db, first.id).remove(canonical.id)
            await db.commit()
            repaired = await service.import_candidate(first.id, unique.id)
            await db.commit()
            assert repaired.user_subscription_id is not None
            assert repaired.user_subscription_id != original_membership_id
            assert await db.get(Subscription, canonical.id) is not None
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


def _headers(username: str) -> dict[str, str]:
    from app.auth import create_access_token

    return {
        "Authorization": f"Bearer {create_access_token(username, must_change_password=False)}"
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_discovery_apis_isolate_scans_candidates_and_manual_import_defaults_no_sync():
    """Public discovery endpoints expose only owned rows and do not sync unless requested."""
    from httpx import ASGITransport, AsyncClient

    from app.config import settings
    from app.database import async_session, engine
    from app.main import app
    from app.models import DiscoveryCandidate, DownloadJob

    adapter = PagedPixivAdapter()
    old_key = settings.remote_credential_key
    settings.remote_credential_key = base64.urlsafe_b64encode(b"d" * 32).decode()
    try:
        async with async_session() as db:
            await _cleanup(db)
            first = await _seed_user(db, "api_first")
            second = await _seed_user(db, "api_second")
            first_account = await _account(db, first, adapter)
            second_account = await _account(db, second, adapter)
            first_candidate = DiscoveryCandidate(
                remote_account_id=first_account.id,
                user_id=first.id,
                source_creator_id="api-first-candidate",
                remote_url="https://www.pixiv.net/users/8101",
                display_name=f"{PREFIX}API First",
                confidence="high",
                state="pending",
            )
            second_candidate = DiscoveryCandidate(
                remote_account_id=second_account.id,
                user_id=second.id,
                source_creator_id="api-second-candidate",
                remote_url="https://www.pixiv.net/users/8102",
                display_name=f"{PREFIX}API Second",
                confidence="high",
                state="pending",
            )
            db.add_all([first_candidate, second_candidate])
            await db.commit()
            first_name, second_name = first.username, second.username
            first_account_id, first_candidate_id = first_account.id, first_candidate.id
            second_candidate_id = second_candidate.id

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first_headers = _headers(first_name)
            second_headers = _headers(second_name)
            candidates = await client.get(
                "/api/v1/discovery/candidates", headers=first_headers
            )
            assert candidates.status_code == 200, candidates.text
            assert candidates.json()["total"] == 1
            assert candidates.json()["items"][0]["id"] == str(first_candidate_id)

            foreign_action = await client.post(
                "/api/v1/discovery/candidates/batch-actions",
                json={"ids": [str(second_candidate_id)], "action": "dismiss"},
                headers=first_headers,
            )
            assert foreign_action.status_code == 404

            scan = await client.post(
                "/api/v1/discovery/scans",
                json={"remote_account_id": str(first_account_id)},
                headers=first_headers,
            )
            assert scan.status_code == 201, scan.text
            assert scan.json()["triggering_remote_account_id"] == str(first_account_id)
            assert (
                await client.post(
                    "/api/v1/discovery/scans",
                    json={"remote_account_id": str(first_account_id)},
                    headers=first_headers,
                )
            ).status_code == 409
            assert (
                await client.post(
                    "/api/v1/discovery/scans",
                    json={"remote_account_id": str(first_account_id)},
                    headers=second_headers,
                )
            ).status_code == 404

            imported = await client.post(
                "/api/v1/discovery/candidates/batch-actions",
                json={"ids": [str(first_candidate_id)], "action": "import"},
                headers=first_headers,
            )
            assert imported.status_code == 200, imported.text
            assert imported.json()["items"][0]["state"] == "imported"
            assert imported.json()["immediate_sync"] is False

        async with async_session() as db:
            assert (await db.execute(select(func.count(DownloadJob.id)))).scalar_one() == 0
    finally:
        settings.remote_credential_key = old_key
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()
