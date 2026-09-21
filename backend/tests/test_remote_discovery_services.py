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
            "OR us.user_id IN (SELECT id FROM users WHERE username LIKE :prefix) "
            "OR tr.owner_user_id IN (SELECT id FROM users WHERE username LIKE :prefix))"
        ),
        params,
    )
    await db.execute(
        text(
            "DELETE FROM task_runs WHERE triggering_remote_account_id IN ("
            "SELECT id FROM remote_accounts WHERE user_id IN "
            "(SELECT id FROM users WHERE username LIKE :prefix)) "
            "OR triggering_user_subscription_id IN (SELECT id FROM user_subscriptions WHERE user_id IN "
            "(SELECT id FROM users WHERE username LIKE :prefix)) "
            "OR owner_user_id IN (SELECT id FROM users WHERE username LIKE :prefix)"
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


@pytest.mark.asyncio
async def test_evidence_batch_enforces_x_oauth_concurrency_two():
    """Provider enrichment must not fan a 25-person segment out without bounds."""
    import asyncio

    from app.remote_discovery.contract import RemoteCandidateEvidence, RemoteCandidateIdentity
    from app.services.remote_discovery import RemoteDiscoveryService

    class Adapter:
        source = "x"

        def __init__(self):
            self.active = 0
            self.maximum = 0

        async def enrich_candidate(self, _credentials, identity):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            await asyncio.sleep(0)
            self.active -= 1
            return RemoteCandidateEvidence(
                source="x",
                source_creator_id=identity.source_creator_id,
            )

    adapter = Adapter()
    identities = [
        RemoteCandidateIdentity(
            source="x",
            source_creator_id=str(index),
            profile_url=f"https://x.com/user{index}",
        )
        for index in range(8)
    ]
    results = await RemoteDiscoveryService._enrich_batch(
        adapter, {}, identities, concurrency=2
    )
    assert adapter.maximum == 2
    assert len(results) == 8


@pytest.mark.asyncio
async def test_bilibili_evidence_batch_is_serial_with_three_to_six_second_jitter(monkeypatch):
    """Bilibili evidence calls need the conservative anti-rate-limit cadence."""
    from app.remote_discovery.contract import RemoteCandidateEvidence, RemoteCandidateIdentity
    from app.services import remote_discovery

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    class Adapter:
        source = "bilibili"

        async def enrich_candidate(self, _credentials, identity):
            return RemoteCandidateEvidence(
                source="bilibili",
                source_creator_id=identity.source_creator_id,
            )

    monkeypatch.setattr(remote_discovery.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(remote_discovery.random, "uniform", lambda low, high: 4.5)
    identities = [
        RemoteCandidateIdentity(
            source="bilibili",
            source_creator_id=str(index),
            profile_url=f"https://space.bilibili.com/{index}/dynamic",
        )
        for index in range(3)
    ]
    await remote_discovery.RemoteDiscoveryService._enrich_batch(
        Adapter(), {}, identities, concurrency=1
    )
    assert sleeps == [4.5, 4.5]


def test_discovery_job_turns_persistent_waiting_state_into_delayed_rq_wakeup(monkeypatch):
    """A completed worker slice must be resumed by the queue scheduler, not stranded."""
    from types import SimpleNamespace

    from rq import Retry

    from app.jobs import remote_discovery as job

    async def fake_run(_task_id):
        return SimpleNamespace(
            status="waiting",
            progress_data={"phase": "cooldown", "retry_after_seconds": 75},
        )

    monkeypatch.setattr(job, "_run", fake_run)
    result = job.run_remote_discovery_scan(str(uuid4()))
    assert isinstance(result, Retry)
    assert result.max == 1_000_000
    assert result.intervals == [75]


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
            assert {item.evidence_status for item in current} == {"ready"}
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
async def test_x_snapshot_finalizes_before_resumable_evidence_segments():
    """Re-running the snapshot for every evidence slice would corrupt follow-state authority."""
    from app.database import async_session, engine
    from app.models import DiscoveryCandidate, RemoteAccount
    from app.remote_discovery.contract import (
        DiscoveryPage,
        RemoteCandidateEvidence,
        RemoteCandidateIdentity,
    )
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_discovery import RemoteDiscoveryService

    class SegmentedXAdapter:
        source = "x"

        def __init__(self):
            self.fetch_calls = 0
            self.enriched: list[str] = []

        async def fetch_page(self, credentials, *, selector=None, cursor=None, page_size=100):
            self.fetch_calls += 1
            return DiscoveryPage(
                items=[
                    RemoteCandidateIdentity(
                        source="x",
                        source_creator_id=f"segmented-{index:02d}",
                        profile_url=f"https://x.com/segmented_{index:02d}",
                        username=f"segmented_{index:02d}",
                        metadata={"description": "fixture"},
                    )
                    for index in range(12)
                ],
                done=True,
            )

        async def enrich_candidate(self, credentials, identity):
            self.enriched.append(identity.source_creator_id)
            return RemoteCandidateEvidence(
                source="x",
                source_creator_id=identity.source_creator_id,
                metadata={**dict(identity.metadata), "recent_visual_post": True},
            )

    adapter = SegmentedXAdapter()
    adapters = Registry(adapter)
    try:
        async with async_session() as db:
            await _cleanup(db)
            user = await _seed_user(db, "segmented_x")
            account = await RemoteAccountService(
                db, user.id, vault=_vault(), adapters=adapters
            ).create(
                {
                    "source": "x",
                    "auth_method": "cookie",
                    "credentials": {"cookie": "auth_token=secret; ct0=csrf"},
                    "collection_selectors": [{"kind": "following"}],
                }
            )
            old = DiscoveryCandidate(
                remote_account_id=account.id,
                user_id=user.id,
                source_creator_id="segmented-old",
                state="pending",
                evidence_status="pending",
                is_following=True,
                last_seen_at=datetime.now(timezone.utc) - timedelta(days=2),
            )
            db.add(old)
            service = RemoteDiscoveryService(db, vault=_vault(), adapters=adapters)
            task = await service.create_scan(user.id, account.id)
            await db.commit()

            first = await service.run_scan(task.id)
            assert first.status == "waiting"
            assert first.progress_data["phase"] == "enriching"
            assert first.progress_data["evidence_completed"] == 10
            await db.refresh(old)
            assert old.is_following is False

            second = await service.run_scan(task.id)
            assert second.status == "complete"
            assert second.progress_data["evidence_completed"] == 12
            assert adapter.fetch_calls == 1
            assert len(adapter.enriched) == 12
            statuses = set(
                (
                    await db.execute(
                        select(DiscoveryCandidate.evidence_status).where(
                            DiscoveryCandidate.remote_account_id == account.id,
                            DiscoveryCandidate.source_creator_id.like("segmented-%"),
                            DiscoveryCandidate.source_creator_id != "segmented-old",
                        )
                    )
                ).scalars()
            )
            assert statuses == {"ready"}
            stored_account = await db.get(RemoteAccount, account.id)
            assert stored_account.scan_cursor is None
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_x_evidence_rate_limit_cools_down_without_skipping_candidate_or_damaging_auth():
    """A 429 wakeup must retry the same candidate and preserve discovery credentials."""
    from app.database import async_session, engine
    from app.models import RemoteAccount
    from app.remote_discovery.common import RemoteRateLimited
    from app.remote_discovery.contract import (
        DiscoveryPage,
        RemoteCandidateEvidence,
        RemoteCandidateIdentity,
    )
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_discovery import RemoteDiscoveryService

    class Adapter:
        source = "x"

        def __init__(self):
            self.fetch_calls = 0
            self.enrich_calls = 0

        async def fetch_page(self, _credentials, **_kwargs):
            self.fetch_calls += 1
            return DiscoveryPage(
                items=[RemoteCandidateIdentity(
                    source="x",
                    source_creator_id="cooldown-one",
                    profile_url="https://x.com/cooldown_one",
                    username="cooldown_one",
                )],
                done=True,
            )

        async def enrich_candidate(self, _credentials, identity):
            self.enrich_calls += 1
            if self.enrich_calls == 1:
                raise RemoteRateLimited(75)
            return RemoteCandidateEvidence(
                source="x",
                source_creator_id=identity.source_creator_id,
                metadata={"recent_visual_post": True},
            )

    adapter = Adapter()
    adapters = Registry(adapter)
    try:
        async with async_session() as db:
            await _cleanup(db)
            user = await _seed_user(db, "cooldown_x")
            account = await RemoteAccountService(
                db, user.id, vault=_vault(), adapters=adapters
            ).create({
                "source": "x",
                "auth_method": "cookie",
                "credentials": {"cookie": "auth_token=secret; ct0=csrf"},
                "collection_selectors": [{"kind": "following"}],
            })
            service = RemoteDiscoveryService(db, vault=_vault(), adapters=adapters)
            task = await service.create_scan(user.id, account.id)
            await db.commit()

            waiting = await service.run_scan(task.id)
            assert waiting.status == "waiting"
            assert waiting.progress_data["phase"] == "cooldown"
            assert waiting.progress_data["retry_after_seconds"] == 75
            assert waiting.progress_data["evidence_after_id"] is None
            stored_account = await db.get(RemoteAccount, account.id)
            assert stored_account.auth_status == "untested"

            completed = await service.run_scan(task.id)
            assert completed.status == "complete"
            assert completed.progress_data["evidence_completed"] == 1
            assert adapter.fetch_calls == 1
            assert adapter.enrich_calls == 2
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_protocol_change_stops_after_three_and_completes_partial_snapshot():
    """Repeated response drift is not an auth failure and must leave later evidence pending."""
    from app.database import async_session, engine
    from app.models import RemoteAccount
    from app.remote_discovery.common import MalformedRemoteResponse
    from app.remote_discovery.contract import DiscoveryPage, RemoteCandidateIdentity
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_discovery import RemoteDiscoveryService

    class Adapter:
        source = "x"

        def __init__(self):
            self.calls = 0

        async def fetch_page(self, _credentials, **_kwargs):
            return DiscoveryPage(items=[
                RemoteCandidateIdentity(
                    source="x",
                    source_creator_id=f"protocol-{index}",
                    profile_url=f"https://x.com/protocol_{index}",
                    username=f"protocol_{index}",
                )
                for index in range(4)
            ], done=True)

        async def enrich_candidate(self, _credentials, _identity):
            self.calls += 1
            raise MalformedRemoteResponse("fixture drift")

    adapter = Adapter()
    adapters = Registry(adapter)
    try:
        async with async_session() as db:
            await _cleanup(db)
            user = await _seed_user(db, "protocol_x")
            account = await RemoteAccountService(
                db, user.id, vault=_vault(), adapters=adapters
            ).create({
                "source": "x",
                "auth_method": "cookie",
                "credentials": {"cookie": "auth_token=secret; ct0=csrf"},
                "collection_selectors": [{"kind": "following"}],
            })
            service = RemoteDiscoveryService(db, vault=_vault(), adapters=adapters)
            task = await service.create_scan(user.id, account.id)
            await db.commit()

            completed = await service.run_scan(task.id)
            assert completed.status == "complete"
            assert completed.result_data["status"] == "partial"
            assert completed.progress_data["evidence_failed"] == 3
            assert completed.progress_data["evidence_pending"] == 1
            assert adapter.calls == 3
            stored_account = await db.get(RemoteAccount, account.id)
            assert stored_account.auth_status == "untested"
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
async def test_external_credential_replacement_during_fetch_commits_no_page_or_finalization():
    """A fetched page cannot cross an externally replaced credential generation."""

    import asyncio

    from app.database import async_session, engine
    from app.models import DiscoveryCandidate, RemoteAccount, TaskRun
    from app.remote_discovery.contract import DiscoveryPage, RemoteCandidateIdentity
    from app.services.remote_accounts import (
        RemoteAccountService,
        RemoteCredentialGenerationChanged,
    )
    from app.services.remote_discovery import RemoteDiscoveryService

    class BlockingAdapter(PagedPixivAdapter):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def fetch_page(self, credentials, *, selector=None, cursor=None, page_size=100):
            assert credentials.materialize()["refresh_token"] == "scan-secret"
            self.started.set()
            await self.release.wait()
            return DiscoveryPage(
                items=(
                    RemoteCandidateIdentity(
                        source="pixiv",
                        source_creator_id="generation-mixed-page",
                        profile_url="https://www.pixiv.net/users/94001",
                        display_name="Generation Mixed Page",
                    ),
                ),
                done=True,
            )

    class FinalizationProbe(RemoteDiscoveryService):
        auto_import_called = False

        async def auto_import(self, account):
            self.auto_import_called = True
            return []

    adapter = BlockingAdapter()
    adapters = Registry(adapter)
    scan_task = None
    try:
        async with async_session() as db:
            await _cleanup(db)
            user = await _seed_user(db, "generation_during_fetch")
            account = await _account(db, user, adapter, auto=True)
            old = DiscoveryCandidate(
                remote_account_id=account.id,
                user_id=user.id,
                source_creator_id="generation-old-follow",
                state="pending",
                is_following=True,
                last_seen_at=datetime.now(timezone.utc) - timedelta(days=3),
            )
            db.add(old)
            service = FinalizationProbe(db, vault=_vault(), adapters=adapters)
            task = await service.create_scan(user.id, account.id)
            await db.commit()
            task_id = task.id
            account_id = account.id
            user_id = user.id
            assert task.progress_data["credential_generation"] == 1
            assert task.progress_data["remote_identity"] == {
                "account_id": str(account.id),
                "auth_method": "refresh_token",
                "remote_user_id": None,
                "source": "pixiv",
            }

        async def run():
            async with async_session() as worker_db:
                return await FinalizationProbe(
                    worker_db,
                    vault=_vault(),
                    adapters=adapters,
                ).run_scan(task_id)

        scan_task = asyncio.create_task(run())
        await asyncio.wait_for(adapter.started.wait(), timeout=5)
        async with async_session() as replacement_db:
            await RemoteAccountService(
                replacement_db,
                user_id,
                vault=_vault(),
                adapters=adapters,
            ).update(
                account_id,
                {"credentials": {"refresh_token": "external-replacement-secret"}},
            )
            await replacement_db.commit()
        adapter.release.set()
        with pytest.raises(RemoteCredentialGenerationChanged):
            await asyncio.wait_for(scan_task, timeout=10)

        async with async_session() as db:
            assert (
                await db.execute(
                    select(DiscoveryCandidate.id).where(
                        DiscoveryCandidate.remote_account_id == account_id,
                        DiscoveryCandidate.source_creator_id == "generation-mixed-page",
                    )
                )
            ).scalar_one_or_none() is None
            old = (
                await db.execute(
                    select(DiscoveryCandidate).where(
                        DiscoveryCandidate.remote_account_id == account_id,
                        DiscoveryCandidate.source_creator_id == "generation-old-follow",
                    )
                )
            ).scalar_one()
            assert old.is_following is True
            assert FinalizationProbe.auto_import_called is False
            stored_task = await db.get(TaskRun, task_id)
            assert stored_task.status == "failed"
            assert (await db.get(RemoteAccount, account_id)).credential_generation == 2
    finally:
        adapter.release.set()
        if scan_task is not None and not scan_task.done():
            scan_task.cancel()
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generation_change_between_pages_aborts_and_new_scan_restarts_current_generation():
    """A prior-generation cursor is never resumed after external reauthentication."""

    import asyncio

    from app.database import async_session, engine
    from app.models import DiscoveryCandidate, RemoteAccount, TaskRun
    from app.remote_discovery.contract import DiscoveryPage, RemoteCandidateIdentity
    from app.services.remote_accounts import (
        RemoteAccountService,
        RemoteCredentialGenerationChanged,
    )
    from app.services.remote_discovery import RemoteDiscoveryService

    class BetweenPagesAdapter(PagedPixivAdapter):
        def __init__(self):
            super().__init__()
            self.second_started = asyncio.Event()
            self.release_second = asyncio.Event()

        async def fetch_page(self, credentials, *, selector=None, cursor=None, page_size=100):
            assert credentials.materialize()["refresh_token"] == "scan-secret"
            if not cursor:
                return DiscoveryPage(
                    items=(
                        RemoteCandidateIdentity(
                            source="pixiv",
                            source_creator_id="generation-page-one",
                            profile_url="https://www.pixiv.net/users/95001",
                            display_name="Generation Page One",
                        ),
                    ),
                    done=False,
                    next_cursor={"offset": 1},
                )
            self.second_started.set()
            await self.release_second.wait()
            return DiscoveryPage(
                items=(
                    RemoteCandidateIdentity(
                        source="pixiv",
                        source_creator_id="generation-page-two",
                        profile_url="https://www.pixiv.net/users/95002",
                        display_name="Generation Page Two",
                    ),
                ),
                done=True,
            )

    adapter = BetweenPagesAdapter()
    adapters = Registry(adapter)
    scan_task = None
    try:
        async with async_session() as db:
            await _cleanup(db)
            user = await _seed_user(db, "generation_between_pages")
            account = await _account(db, user, adapter)
            task = await RemoteDiscoveryService(
                db, vault=_vault(), adapters=adapters
            ).create_scan(user.id, account.id)
            await db.commit()
            task_id = task.id
            account_id = account.id
            user_id = user.id

        async def run():
            async with async_session() as worker_db:
                return await RemoteDiscoveryService(
                    worker_db,
                    vault=_vault(),
                    adapters=adapters,
                ).run_scan(task_id)

        scan_task = asyncio.create_task(run())
        await asyncio.wait_for(adapter.second_started.wait(), timeout=5)
        async with async_session() as replacement_db:
            await RemoteAccountService(
                replacement_db,
                user_id,
                vault=_vault(),
                adapters=adapters,
            ).update(
                account_id,
                {"credentials": {"refresh_token": "between-page-replacement"}},
            )
            await replacement_db.commit()
        adapter.release_second.set()
        with pytest.raises(RemoteCredentialGenerationChanged):
            await asyncio.wait_for(scan_task, timeout=10)

        async with async_session() as db:
            candidates = list(
                (
                    await db.execute(
                        select(DiscoveryCandidate.source_creator_id).where(
                            DiscoveryCandidate.remote_account_id == account_id,
                            DiscoveryCandidate.source_creator_id.in_(
                                {"generation-page-one", "generation-page-two"}
                            ),
                        )
                    )
                ).scalars()
            )
            assert candidates == ["generation-page-one"]
            stored_task = await db.get(TaskRun, task_id)
            assert stored_task.progress_data["cursor"] == {"offset": 1}
            new_scan = await RemoteDiscoveryService(
                db,
                vault=_vault(),
                adapters=adapters,
            ).create_scan(user_id, account_id)
            assert new_scan.progress_data["credential_generation"] == 2
            assert new_scan.progress_data["cursor"] is None
            assert new_scan.progress_data["pages_completed"] == 0
    finally:
        adapter.release_second.set()
        if scan_task is not None and not scan_task.done():
            scan_task.cancel()
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generation_change_before_worker_claim_fails_old_task_and_allows_full_scan():
    """A queued task is pinned at creation and cannot adopt replacement credentials."""

    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services.remote_accounts import (
        RemoteAccountService,
        RemoteCredentialGenerationChanged,
    )
    from app.services.remote_discovery import RemoteDiscoveryService

    adapter = PagedPixivAdapter()
    adapters = Registry(adapter)
    try:
        async with async_session() as db:
            await _cleanup(db)
            user = await _seed_user(db, "generation_before_claim")
            account = await _account(db, user, adapter)
            service = RemoteDiscoveryService(db, vault=_vault(), adapters=adapters)
            old_task = await service.create_scan(user.id, account.id)
            await db.commit()
            user_id = user.id
            account_id = account.id
            old_task_id = old_task.id

            await RemoteAccountService(
                db,
                user_id,
                vault=_vault(),
                adapters=adapters,
            ).update(
                account_id,
                {"credentials": {"refresh_token": "before-claim-replacement"}},
            )
            await db.commit()

            with pytest.raises(RemoteCredentialGenerationChanged):
                await service.run_scan(old_task_id)

            stored = await db.get(TaskRun, old_task_id, populate_existing=True)
            assert stored.status == "failed"
            assert stored.reason_code == "remote_credential_changed"
            replacement = await service.create_scan(user_id, account_id)
            assert replacement.progress_data["credential_generation"] == 2
            assert replacement.progress_data["cursor"] is None
            assert replacement.progress_data["pages_completed"] == 0
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scan_list_retains_owned_history_after_remote_account_hard_delete():
    """Durable task ownership must not depend on deletable account provenance."""

    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_discovery import RemoteDiscoveryService

    adapter = PagedPixivAdapter()
    adapters = Registry(adapter)
    try:
        async with async_session() as db:
            await _cleanup(db)
            owner = await _seed_user(db, "scan_history_owner")
            peer = await _seed_user(db, "scan_history_peer")
            account = await _account(db, owner, adapter)
            discovery = RemoteDiscoveryService(db, vault=_vault(), adapters=adapters)
            task = await discovery.create_scan(owner.id, account.id)
            task.status = "failed"
            await db.commit()
            task_id = task.id
            owner_id = owner.id
            peer_id = peer.id

            await RemoteAccountService(
                db,
                owner_id,
                vault=_vault(),
                adapters=adapters,
            ).delete(account.id)
            await db.commit()
            stored = await db.get(TaskRun, task_id, populate_existing=True)
            assert stored.triggering_remote_account_id is None
            assert stored.owner_user_id == owner_id

            total, items = await discovery.list_scans(owner_id)
            assert total == 1
            assert [item.id for item in items] == [task_id]
            peer_total, peer_items = await discovery.list_scans(peer_id)
            assert peer_total == 0
            assert peer_items == []
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("vault_mode", ["missing", "wrong"])
async def test_scan_credential_setup_failure_is_terminal_and_recoverable(
    monkeypatch, vault_mode
):
    """Pre-provider credential failures must not strand a claimed scan as running."""

    from app.config import settings
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services.remote_credentials import CredentialVault
    from app.services.remote_discovery import RemoteDiscoveryService

    adapter = PagedPixivAdapter()
    monkeypatch.setattr(settings, "remote_discovery_private_members_enabled", True)
    monkeypatch.setattr(settings, "remote_discovery_pixiv_preview_enabled", True)
    try:
        async with async_session() as db:
            await _cleanup(db)
            user = await _seed_user(db, f"credential-{vault_mode}")
            account = await _account(db, user, adapter)
            task = await RemoteDiscoveryService(db, adapters=Registry(adapter)).create_scan(
                user.id, account.id
            )
            await db.commit()
            task_id = task.id

            if vault_mode == "missing":
                monkeypatch.setattr(settings, "remote_credential_key", "")
                failing_vault = None
            else:
                failing_vault = CredentialVault(
                    base64.urlsafe_b64encode(b"w" * 32).decode()
                )

            with pytest.raises((RuntimeError, ValueError)):
                await RemoteDiscoveryService(
                    db, vault=failing_vault, adapters=Registry(adapter)
                ).run_scan(task_id)

            stored_task = await db.get(TaskRun, task_id, populate_existing=True)
            assert stored_task.status == "failed"
            assert stored_task.reason_code == "remote_discovery_failed"
            assert "scan-secret" not in str(stored_task.error_log)
            assert adapter.calls == []

            # RQ recovery (or an operator retry after fixing configuration) may
            # reclaim the terminal task; no stale `running` row blocks it.
            stored_task.status = "recovering"
            await db.commit()
            completed = await RemoteDiscoveryService(
                db, vault=_vault(), adapters=Registry(adapter)
            ).run_scan(task_id)
            assert completed.status == "complete"
            assert adapter.calls
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
            stored_account.credential_generation = 7
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
            assert stored_account.credential_generation == 7
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reimport_after_last_member_reactivates_default_schedule_and_source_due():
    """An imported candidate must not inherit a disabled aggregate cache."""
    from app.database import async_session, engine
    from app.models import (
        DiscoveryCandidate,
        RemoteAccount,
        Subscription,
        SubscriptionSource,
        SystemSetting,
        UserSubscription,
        UserSubscriptionSource,
    )
    from app.remote_discovery.contract import RemoteCandidateIdentity
    from app.services.remote_discovery import RemoteDiscoveryService
    from app.services.subscription_membership import SubscriptionMembershipService

    adapter = PagedPixivAdapter()
    original_defaults = None
    created_defaults = False
    try:
        async with async_session() as db:
            await _cleanup(db)
            defaults = (
                await db.execute(
                    select(SystemSetting).where(SystemSetting.key == "subscription_defaults")
                )
            ).scalar_one_or_none()
            if defaults is None:
                defaults = SystemSetting(key="subscription_defaults", value={})
                db.add(defaults)
                created_defaults = True
            else:
                original_defaults = dict(defaults.value or {})
            defaults.value = {
                **(defaults.value or {}),
                "schedule_mode": "interval",
                "default_sync_interval_hours": 13,
            }
            user = await _seed_user(db, "reactivate")
            account = await _account(db, user, adapter)
            await db.commit()

            service = RemoteDiscoveryService(
                db, vault=_vault(), adapters=Registry(adapter)
            )
            stored_account = await db.get(RemoteAccount, account.id)
            candidate = await service.upsert_candidate(
                stored_account,
                RemoteCandidateIdentity(
                    source="pixiv",
                    source_creator_id="reactivate-creator",
                    profile_url="https://www.pixiv.net/users/66331",
                    display_name="Reactivate Creator",
                    metadata={"has_illustration_preview": True},
                ),
                seen_at=datetime.now(timezone.utc),
            )
            imported = await service.import_candidate(user.id, candidate.id)
            await db.commit()
            subscription_id = imported.subscription_id
            membership_id = imported.user_subscription_id
            binding = (
                await db.execute(
                    select(UserSubscriptionSource).where(
                        UserSubscriptionSource.user_subscription_id == membership_id
                    )
                )
            ).scalar_one()
            original_binding_id = binding.id
            await SubscriptionMembershipService(db, user.id).remove_source(
                subscription_id, binding.subscription_source_id
            )
            await db.commit()

            # Re-import recreates a missing binding but retains the intact
            # membership row and applies a fresh due marker to the new source.
            rebound = await service.import_candidate(user.id, candidate.id)
            await db.commit()
            assert rebound.user_subscription_id == membership_id
            rebound_binding = (
                await db.execute(
                    select(UserSubscriptionSource).where(
                        UserSubscriptionSource.user_subscription_id == membership_id
                    )
                )
            ).scalar_one()
            assert rebound_binding.id != original_binding_id
            assert rebound_binding.is_enabled is True
            assert rebound_binding.next_sync_at is not None

            await SubscriptionMembershipService(db, user.id).remove(subscription_id)
            await db.commit()
            canonical = await db.get(Subscription, subscription_id)
            canonical_source = (
                await db.execute(
                    select(SubscriptionSource).where(
                        SubscriptionSource.subscription_id == subscription_id
                    )
                )
            ).scalar_one()
            assert canonical.is_active is False
            assert canonical.sync_enabled is False
            assert canonical_source.is_enabled is False

            repaired = await service.import_candidate(user.id, candidate.id)
            await db.commit()
            repaired_member = await db.get(UserSubscription, repaired.user_subscription_id)
            repaired_binding = (
                await db.execute(
                    select(UserSubscriptionSource).where(
                        UserSubscriptionSource.user_subscription_id == repaired_member.id
                    )
                )
            ).scalar_one()
            assert repaired_member.is_active is True
            assert repaired_member.sync_enabled is True
            assert repaired_member.schedule_mode == "interval"
            assert repaired_member.sync_interval_hours == 13
            assert repaired_binding.is_enabled is True
            assert repaired_binding.next_sync_at is not None
            assert repaired.user_subscription_id == repaired_member.id

            # A manual system default remains active but intentionally has no
            # automatic due time.
            await SubscriptionMembershipService(db, user.id).remove(subscription_id)
            defaults = (
                await db.execute(
                    select(SystemSetting).where(SystemSetting.key == "subscription_defaults")
                )
            ).scalar_one()
            defaults.value = {**(defaults.value or {}), "schedule_mode": "manual"}
            await db.commit()
            manual = await service.import_candidate(user.id, candidate.id)
            await db.commit()
            manual_member = await db.get(UserSubscription, manual.user_subscription_id)
            manual_binding = (
                await db.execute(
                    select(UserSubscriptionSource).where(
                        UserSubscriptionSource.user_subscription_id == manual_member.id
                    )
                )
            ).scalar_one()
            assert manual_member.is_active is True
            assert manual_member.sync_enabled is False
            assert manual_member.schedule_mode == "manual"
            assert manual_binding.is_enabled is True
            assert manual_binding.next_sync_at is None
    finally:
        async with async_session() as db:
            defaults = (
                await db.execute(
                    select(SystemSetting).where(SystemSetting.key == "subscription_defaults")
                )
            ).scalar_one_or_none()
            if defaults is not None:
                if created_defaults:
                    await db.delete(defaults)
                else:
                    defaults.value = original_defaults or {}
                await db.commit()
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_import_preserves_intact_private_membership_and_source_policy():
    """An already imported intact binding is a private-policy no-op."""
    from app.database import async_session, engine
    from app.models import RemoteAccount, UserSubscription, UserSubscriptionSource
    from app.remote_discovery.contract import RemoteCandidateIdentity
    from app.services.remote_discovery import RemoteDiscoveryService

    adapter = PagedPixivAdapter()
    fixed_due = datetime(2032, 4, 5, 6, 7, tzinfo=timezone.utc)
    try:
        async with async_session() as db:
            await _cleanup(db)
            user = await _seed_user(db, "duplicate_policy")
            account = await _account(db, user, adapter)
            await db.commit()
            service = RemoteDiscoveryService(db, vault=_vault(), adapters=Registry(adapter))
            candidate = await service.upsert_candidate(
                await db.get(RemoteAccount, account.id),
                RemoteCandidateIdentity(
                    source="pixiv",
                    source_creator_id="duplicate-policy-creator",
                    profile_url="https://www.pixiv.net/users/85001",
                    display_name=f"{PREFIX}Duplicate Policy Artist",
                    metadata={"has_illustration_preview": True},
                ),
                seen_at=datetime.now(timezone.utc),
            )
            imported = await service.import_candidate(user.id, candidate.id)
            await db.commit()
            member = await db.get(UserSubscription, imported.user_subscription_id)
            binding = (
                await db.execute(
                    select(UserSubscriptionSource).where(
                        UserSubscriptionSource.user_subscription_id == member.id
                    )
                )
            ).scalar_one()
            membership_id = member.id
            binding_id = binding.id
            member.name = "My private artist label"
            member.is_active = False
            member.sync_enabled = False
            member.sync_interval_hours = 41
            member.schedule_mode = "manual"
            member.schedule_rule = None
            member.scheduled_times = "04:15"
            binding.is_enabled = False
            binding.auth_healthy = False
            binding.auth_status = "reauth_required"
            binding.auth_error_reason = "expired_session"
            binding.next_sync_at = fixed_due
            await db.commit()

            repeated = await service.import_candidate(user.id, candidate.id)
            await db.commit()
            assert repeated.user_subscription_id == membership_id
            stored_member = await db.get(UserSubscription, membership_id)
            stored_binding = await db.get(UserSubscriptionSource, binding_id)
            assert stored_member.name == "My private artist label"
            assert stored_member.is_active is False
            assert stored_member.sync_enabled is False
            assert stored_member.sync_interval_hours == 41
            assert stored_member.schedule_mode == "manual"
            assert stored_member.schedule_rule is None
            assert stored_member.scheduled_times == "04:15"
            assert stored_binding.is_enabled is False
            assert stored_binding.auth_healthy is False
            assert stored_binding.auth_status == "reauth_required"
            assert stored_binding.auth_error_reason == "expired_session"
            assert stored_binding.next_sync_at == fixed_due
            assert stored_binding.remote_account_id == account.id
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_users_import_same_remote_identity_converges_shared_rows():
    """Concurrent imports serialize the shared identity while retaining private members."""
    import asyncio

    from app.database import async_session, engine
    from app.models import (
        DiscoveryCandidate,
        RemoteAccount,
        SourceCreator,
        Subscription,
        SubscriptionSource,
        UserSubscription,
    )
    from app.remote_discovery.contract import RemoteCandidateIdentity
    from app.services.remote_discovery import RemoteDiscoveryService

    adapter = PagedPixivAdapter()
    adapters = Registry(adapter)
    arrivals = 0
    arrival_lock = asyncio.Lock()
    both_ready = asyncio.Event()

    class RacingImportService(RemoteDiscoveryService):
        async def _owned_account(self, *args, **kwargs):
            nonlocal arrivals
            account = await super()._owned_account(*args, **kwargs)
            async with arrival_lock:
                arrivals += 1
                if arrivals == 2:
                    both_ready.set()
            await asyncio.wait_for(both_ready.wait(), timeout=5)
            return account

    try:
        async with async_session() as db:
            await _cleanup(db)
            first = await _seed_user(db, "race_first")
            second = await _seed_user(db, "race_second")
            first_account = await _account(db, first, adapter)
            second_account = await _account(db, second, adapter)
            await db.commit()

            service = RemoteDiscoveryService(db, vault=_vault(), adapters=adapters)
            first_candidate = await service.upsert_candidate(
                await db.get(RemoteAccount, first_account.id),
                RemoteCandidateIdentity(
                    source="pixiv",
                    source_creator_id="concurrent-shared-identity",
                    profile_url="https://www.pixiv.net/users/8119",
                    display_name=f"{PREFIX}Concurrent Shared",
                    metadata={"has_illustration_preview": True},
                ),
                seen_at=datetime.now(timezone.utc),
            )
            second_candidate = await service.upsert_candidate(
                await db.get(RemoteAccount, second_account.id),
                RemoteCandidateIdentity(
                    source="pixiv",
                    source_creator_id="concurrent-shared-identity",
                    profile_url="https://www.pixiv.net/users/8119",
                    display_name=f"{PREFIX}Concurrent Shared",
                    metadata={"has_illustration_preview": True},
                ),
                seen_at=datetime.now(timezone.utc),
            )
            await db.commit()
            identifiers = (
                (first.id, first_candidate.id),
                (second.id, second_candidate.id),
            )

        async def import_one(user_id, candidate_id):
            async with async_session() as worker_db:
                candidate = await RacingImportService(
                    worker_db, vault=_vault(), adapters=adapters
                ).import_candidate(user_id, candidate_id)
                await worker_db.commit()
                return candidate.subscription_id

        subscription_ids = await asyncio.wait_for(
            asyncio.gather(*(import_one(*item) for item in identifiers)),
            timeout=15,
        )
        assert subscription_ids[0] == subscription_ids[1]

        async with async_session() as db:
            source_creators = list(
                (
                    await db.execute(
                        select(SourceCreator).where(
                            SourceCreator.source == "pixiv",
                            SourceCreator.source_creator_id
                            == "concurrent-shared-identity",
                        )
                    )
                ).scalars()
            )
            assert len(source_creators) == 1
            creator_id = source_creators[0].creator_id
            assert (
                await db.execute(
                    select(func.count(Subscription.id)).where(
                        Subscription.creator_id == creator_id
                    )
                )
            ).scalar_one() == 1
            subscription_id = subscription_ids[0]
            assert (
                await db.execute(
                    select(func.count(SubscriptionSource.id)).where(
                        SubscriptionSource.subscription_id == subscription_id,
                        SubscriptionSource.source_creator_id
                        == "concurrent-shared-identity",
                    )
                )
            ).scalar_one() == 1
            members = list(
                (
                    await db.execute(
                        select(UserSubscription).where(
                            UserSubscription.subscription_id == subscription_id
                        )
                    )
                ).scalars()
            )
            assert {member.user_id for member in members} == {
                identifiers[0][0],
                identifiers[1][0],
            }
            assert (
                await db.execute(
                    select(func.count(DiscoveryCandidate.id)).where(
                        DiscoveryCandidate.subscription_id == subscription_id,
                        DiscoveryCandidate.state == "imported",
                    )
                )
            ).scalar_one() == 2
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_cross_site_imports_converge_on_one_creator_subscription():
    """Different remote identities matched to one Creator share one canonical subscription."""
    import asyncio

    from app.database import async_session, engine
    from app.models import (
        Creator,
        DiscoveryCandidate,
        RemoteAccount,
        SourceCreator,
        Subscription,
        SubscriptionSource,
        UserSubscription,
    )
    from app.remote_discovery.contract import RemoteCandidateIdentity
    from app.services.remote_discovery import RemoteDiscoveryService

    adapter = PagedPixivAdapter()
    adapters = Registry(adapter)
    arrivals = 0
    arrival_lock = asyncio.Lock()
    both_ready = asyncio.Event()

    class RacingCrossSiteImportService(RemoteDiscoveryService):
        async def _owned_account(self, *args, **kwargs):
            nonlocal arrivals
            account = await super()._owned_account(*args, **kwargs)
            async with arrival_lock:
                arrivals += 1
                if arrivals == 2:
                    both_ready.set()
            await asyncio.wait_for(both_ready.wait(), timeout=5)
            return account

    try:
        async with async_session() as db:
            await _cleanup(db)
            first = await _seed_user(db, "cross_site_first")
            second = await _seed_user(db, "cross_site_second")
            creator = Creator(name=f"{PREFIX}Cross Site Shared Creator")
            db.add(creator)
            await db.flush()
            pixiv_account = RemoteAccount(
                user_id=first.id,
                source="pixiv",
                auth_method="refresh_token",
                auth_status="healthy",
                credential_ciphertext="test-only-pixiv-ciphertext",
                credential_generation=1,
            )
            bilibili_account = RemoteAccount(
                user_id=second.id,
                source="bilibili",
                auth_method="sessdata",
                auth_status="healthy",
                credential_ciphertext="test-only-bilibili-ciphertext",
                credential_generation=1,
            )
            db.add_all([pixiv_account, bilibili_account])
            await db.flush()
            service = RemoteDiscoveryService(db, vault=_vault(), adapters=adapters)
            pixiv_candidate = await service.upsert_candidate(
                pixiv_account,
                RemoteCandidateIdentity(
                    source="pixiv",
                    source_creator_id="cross-site-pixiv",
                    profile_url="https://www.pixiv.net/users/83001",
                    display_name="Cross Site Pixiv",
                    metadata={"has_illustration_preview": True},
                ),
                seen_at=datetime.now(timezone.utc),
                additional_creator_ids={creator.id},
            )
            bilibili_candidate = await service.upsert_candidate(
                bilibili_account,
                RemoteCandidateIdentity(
                    source="bilibili",
                    source_creator_id="cross-site-bilibili",
                    profile_url="https://space.bilibili.com/83002",
                    display_name="Cross Site Bilibili",
                    metadata={"has_art_bio": True},
                ),
                seen_at=datetime.now(timezone.utc),
                additional_creator_ids={creator.id},
            )
            await db.commit()
            creator_id = creator.id
            identifiers = (
                (first.id, pixiv_candidate.id),
                (second.id, bilibili_candidate.id),
            )

        async def import_one(user_id, candidate_id):
            async with async_session() as worker_db:
                candidate = await RacingCrossSiteImportService(
                    worker_db, vault=_vault(), adapters=adapters
                ).import_candidate(user_id, candidate_id)
                await worker_db.commit()
                return candidate.subscription_id

        subscription_ids = await asyncio.wait_for(
            asyncio.gather(*(import_one(*item) for item in identifiers)), timeout=15
        )
        assert subscription_ids[0] == subscription_ids[1]

        async with async_session() as db:
            subscription_id = subscription_ids[0]
            assert (
                await db.execute(
                    select(func.count(Subscription.id)).where(
                        Subscription.creator_id == creator_id
                    )
                )
            ).scalar_one() == 1
            assert (
                await db.execute(
                    select(func.count(SourceCreator.id)).where(
                        SourceCreator.creator_id == creator_id,
                        SourceCreator.source.in_({"pixiv", "bilibili"}),
                    )
                )
            ).scalar_one() == 2
            assert set(
                (
                    await db.execute(
                        select(SubscriptionSource.source).where(
                            SubscriptionSource.subscription_id == subscription_id
                        )
                    )
                ).scalars()
            ) == {"pixiv", "bilibili"}
            assert (
                await db.execute(
                    select(func.count(UserSubscription.id)).where(
                        UserSubscription.subscription_id == subscription_id
                    )
                )
            ).scalar_one() == 2
            assert (
                await db.execute(
                    select(func.count(DiscoveryCandidate.id)).where(
                        DiscoveryCandidate.subscription_id == subscription_id,
                        DiscoveryCandidate.state == "imported",
                    )
                )
            ).scalar_one() == 2
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_conflict_resolutions_reject_incompatible_identity_mapping():
    """One remote identity cannot be concurrently resolved to two different Creators."""
    import asyncio

    from app.database import async_session, engine
    from app.models import Creator, DiscoveryCandidate, RemoteAccount, SourceCreator
    from app.remote_discovery.contract import RemoteCandidateIdentity
    from app.services.remote_discovery import RemoteDiscoveryService

    adapter = PagedPixivAdapter()
    adapters = Registry(adapter)
    arrivals = 0
    arrival_lock = asyncio.Lock()
    both_ready = asyncio.Event()

    class RacingResolveService(RemoteDiscoveryService):
        async def _owned_account(self, *args, **kwargs):
            nonlocal arrivals
            account = await super()._owned_account(*args, **kwargs)
            async with arrival_lock:
                arrivals += 1
                if arrivals == 2:
                    both_ready.set()
            await asyncio.wait_for(both_ready.wait(), timeout=5)
            return account

    try:
        async with async_session() as db:
            await _cleanup(db)
            first = await _seed_user(db, "resolve_race_first")
            second = await _seed_user(db, "resolve_race_second")
            first_creator = Creator(name=f"{PREFIX}Resolve Target A")
            second_creator = Creator(name=f"{PREFIX}Resolve Target B")
            db.add_all([first_creator, second_creator])
            await db.flush()
            first_account = RemoteAccount(
                user_id=first.id,
                source="pixiv",
                auth_method="refresh_token",
                auth_status="healthy",
            )
            second_account = RemoteAccount(
                user_id=second.id,
                source="pixiv",
                auth_method="refresh_token",
                auth_status="healthy",
            )
            db.add_all([first_account, second_account])
            await db.flush()
            service = RemoteDiscoveryService(db, vault=_vault(), adapters=adapters)

            async def conflict_candidate(account, suffix):
                return await service.upsert_candidate(
                    account,
                    RemoteCandidateIdentity(
                        source="pixiv",
                        source_creator_id="concurrent-resolution-identity",
                        profile_url="https://www.pixiv.net/users/84001",
                        display_name=f"Concurrent Resolve {suffix}",
                    ),
                    seen_at=datetime.now(timezone.utc),
                    additional_creator_ids={first_creator.id, second_creator.id},
                )

            first_candidate = await conflict_candidate(first_account, "A")
            second_candidate = await conflict_candidate(second_account, "B")
            assert first_candidate.state == second_candidate.state == "conflict"
            await db.commit()
            attempts = (
                (first.id, first_candidate.id, first_creator.id),
                (second.id, second_candidate.id, second_creator.id),
            )

        async def resolve_one(user_id, candidate_id, creator_id):
            async with async_session() as worker_db:
                try:
                    await RacingResolveService(
                        worker_db, vault=_vault(), adapters=adapters
                    ).resolve_candidate(user_id, candidate_id, creator_id=creator_id)
                    await worker_db.commit()
                    return ("resolved", creator_id)
                except Exception as exc:
                    await worker_db.rollback()
                    return exc

        results = await asyncio.wait_for(
            asyncio.gather(*(resolve_one(*attempt) for attempt in attempts)), timeout=15
        )
        successes = [item for item in results if isinstance(item, tuple)]
        failures = [item for item in results if isinstance(item, Exception)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], ValueError)
        assert "already resolved" in str(failures[0]).lower()

        async with async_session() as db:
            source_creator = (
                await db.execute(
                    select(SourceCreator).where(
                        SourceCreator.source == "pixiv",
                        SourceCreator.source_creator_id == "concurrent-resolution-identity",
                    )
                )
            ).scalar_one()
            winning_creator_id = successes[0][1]
            assert source_creator.creator_id == winning_creator_id
            candidates = list(
                (
                    await db.execute(
                        select(DiscoveryCandidate).where(
                            DiscoveryCandidate.id.in_(
                                [attempts[0][1], attempts[1][1]]
                            )
                        )
                    )
                ).scalars()
            )
            assert {candidate.state for candidate in candidates} == {"pending", "conflict"}
            resolved = next(candidate for candidate in candidates if candidate.state == "pending")
            assert resolved.candidate_metadata["resolved_creator_id"] == str(
                winning_creator_id
            )
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
async def test_candidate_local_match_filter_applies_before_count_and_pagination():
    """Matches from an unfiltered second page become the filtered first page."""
    from httpx import ASGITransport, AsyncClient

    from app.database import async_session, engine
    from app.main import app
    from app.models import Creator, DiscoveryCandidate, Subscription
    from app.services.remote_discovery import RemoteDiscoveryService

    adapter = PagedPixivAdapter()
    try:
        async with async_session() as db:
            await _cleanup(db)
            user = await _seed_user(db, "local_match")
            account = await _account(db, user, adapter)
            creator = Creator(name=f"{PREFIX}local_match_creator")
            db.add(creator)
            await db.flush()
            subscription = Subscription(
                creator_id=creator.id,
                name=f"{PREFIX}local_match_subscription",
            )
            db.add(subscription)
            await db.flush()
            newest = datetime.now(timezone.utc)
            unmatched = [
                DiscoveryCandidate(
                    remote_account_id=account.id,
                    user_id=user.id,
                    source_creator_id=f"local-unmatched-{index:02d}",
                    candidate_metadata={"local_creator_ids": []},
                    confidence="high",
                    state="pending",
                    updated_at=newest - timedelta(seconds=index),
                )
                for index in range(25)
            ]
            malformed = [
                DiscoveryCandidate(
                    remote_account_id=account.id,
                    user_id=user.id,
                    source_creator_id=f"local-malformed-{shape}",
                    candidate_metadata=metadata,
                    confidence="high",
                    state="pending",
                    updated_at=newest - timedelta(seconds=100 + index),
                )
                for index, (shape, metadata) in enumerate(
                    (
                        ("object", {"local_creator_ids": {"unexpected": True}}),
                        ("scalar", {"local_creator_ids": "unexpected"}),
                        ("null", {"local_creator_ids": None}),
                        ("missing", {}),
                    )
                )
            ]
            metadata_match = DiscoveryCandidate(
                remote_account_id=account.id,
                user_id=user.id,
                source_creator_id="local-metadata-match",
                candidate_metadata={"local_creator_ids": [str(creator.id)]},
                confidence="high",
                state="pending",
                updated_at=newest - timedelta(seconds=110),
            )
            conflict_match = DiscoveryCandidate(
                remote_account_id=account.id,
                user_id=user.id,
                source_creator_id="local-conflict-match",
                candidate_metadata={"local_creator_ids": [str(creator.id), str(uuid4())]},
                confidence="high",
                state="conflict",
                updated_at=newest - timedelta(seconds=111),
            )
            subscription_match = DiscoveryCandidate(
                remote_account_id=account.id,
                user_id=user.id,
                source_creator_id="local-subscription-match",
                candidate_metadata=None,
                confidence="high",
                state="imported",
                subscription_id=subscription.id,
                updated_at=newest - timedelta(seconds=112),
            )
            db.add_all(
                [
                    *unmatched,
                    *malformed,
                    metadata_match,
                    conflict_match,
                    subscription_match,
                ]
            )
            await db.commit()
            username = user.username

            service = RemoteDiscoveryService(db)
            unfiltered_total, second_page = await service.list_candidates(
                user.id,
                offset=25,
                limit=25,
            )
            assert unfiltered_total == 32
            assert {item.id for item in second_page} >= {
                metadata_match.id,
                conflict_match.id,
                subscription_match.id,
            }
            matched_total, matched = await service.list_candidates(
                user.id,
                local_match=True,
                offset=0,
                limit=25,
            )
            assert matched_total == 3
            assert {item.id for item in matched} == {
                metadata_match.id,
                conflict_match.id,
                subscription_match.id,
            }
            unmatched_total, unmatched_page_two = await service.list_candidates(
                user.id,
                local_match=False,
                offset=25,
                limit=25,
            )
            assert unmatched_total == 29
            assert {item.id for item in unmatched_page_two} == {
                item.id for item in malformed
            }
            empty_total, empty_page = await service.list_candidates(
                user.id,
                local_match=True,
                offset=25,
                limit=25,
            )
            assert empty_total == 3
            assert empty_page == []

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = _headers(username)
            matched_response = await client.get(
                "/api/v1/discovery/candidates",
                params={"local_match": "true", "offset": 0, "limit": 25},
                headers=headers,
            )
            assert matched_response.status_code == 200, matched_response.text
            assert matched_response.json()["total"] == 3
            assert {
                item["source_creator_id"] for item in matched_response.json()["items"]
            } == {
                "local-metadata-match",
                "local-conflict-match",
                "local-subscription-match",
            }
            conflict_response = await client.get(
                "/api/v1/discovery/candidates",
                params={"local_match": "true", "state": "conflict"},
                headers=headers,
            )
            assert conflict_response.status_code == 200
            assert conflict_response.json()["total"] == 1
            assert conflict_response.json()["items"][0]["source_creator_id"] == (
                "local-conflict-match"
            )
            unmatched_conflict = await client.get(
                "/api/v1/discovery/candidates",
                params={"local_match": "false", "state": "conflict"},
                headers=headers,
            )
            assert unmatched_conflict.status_code == 200
            assert unmatched_conflict.json() == {"total": 0, "items": []}
            invalid = await client.get(
                "/api/v1/discovery/candidates",
                params={"local_match": "not-a-boolean"},
                headers=headers,
            )
            assert invalid.status_code == 422
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


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
