from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from io import StringIO
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select, text


async def _clear_reconciliation_tables(db) -> None:
    await db.execute(text("""
        TRUNCATE
            repository_sync_receipts,
            search_index_states,
            task_events,
            task_runs,
            storage_artifacts,
            import_jobs,
            download_jobs,
            work_source_tags,
            work_tags,
            asset_sources,
            assets,
            work_sources,
            works,
            source_creators,
            subscription_sources,
            subscriptions,
            creators
        RESTART IDENTITY CASCADE
    """))
    await db.commit()


async def _repository_fixture(
    db,
    *,
    source: str = "x",
    source_creator_id: str | None = "opaque-target-id",
    source_url: str = "https://x.com/target_handle",
    other_source_creator_id: str | None = "opaque-other-id",
    other_source_url: str = "https://x.com/other_handle",
):
    from app.models import Creator, DownloadJob, Subscription, SubscriptionSource

    creator = Creator(name="reconcile-creator", display_name="Reconcile Creator")
    db.add(creator)
    await db.flush()
    subscription = Subscription(creator_id=creator.id, name="Reconcile Creator")
    db.add(subscription)
    await db.flush()
    repository = SubscriptionSource(
        subscription_id=subscription.id,
        source=source,
        source_creator_id=source_creator_id,
        source_url=source_url,
    )
    other_repository = SubscriptionSource(
        subscription_id=subscription.id,
        source=source,
        source_creator_id=other_source_creator_id,
        source_url=other_source_url,
    )
    db.add_all([repository, other_repository])
    await db.flush()
    current = DownloadJob(
        subscription_id=subscription.id,
        subscription_source_id=repository.id,
        source=source,
        source_url=repository.source_url,
        status="downloaded",
    )
    historical = DownloadJob(
        subscription_id=subscription.id,
        subscription_source_id=repository.id,
        source=source,
        source_url=repository.source_url,
        status="complete",
    )
    manual = DownloadJob(
        subscription_id=subscription.id,
        source=source,
        source_url=source_url,
        status="downloaded",
    )
    db.add_all([current, historical, manual])
    await db.flush()
    return subscription, repository, other_repository, current, historical, manual


def _artifact(
    *,
    path: str,
    work_id: str,
    creator_dir: str,
    job_id,
    source: str = "x",
    state: str = "new",
    **extra,
):
    from app.models import StorageArtifact

    return StorageArtifact(
        storage_root="downloads",
        file_path=path,
        source=source,
        creator_dir=creator_dir,
        source_work_id=work_id,
        file_name=path.rsplit("/", 1)[-1],
        artifact_type="metadata_json",
        download_job_id=job_id,
        state=state,
        **extra,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repository_reconciliation_recovers_zero_delta_backlog_without_stealing_live_leases():
    """A zero-network repository resync must enqueue the safe ledger backlog."""
    from app.database import async_session, engine
    from app.models import ImportJob, StorageArtifact, Work, WorkSource
    from app.services.repository_artifact_reconciliation import (
        reconcile_repository_artifacts,
    )

    now = datetime.now(timezone.utc)
    try:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
            _subscription, _repository, other_repository, current, historical, _manual = await _repository_fixture(db)
            stale_import = ImportJob(download_job_id=historical.id, status="failed")
            live_import = ImportJob(download_job_id=historical.id, status="running")
            existing_work = Work(title="already imported")
            db.add_all([stale_import, live_import, existing_work])
            await db.flush()
            db.add(WorkSource(work_id=existing_work.id, source="x", source_work_id="existing-104"))
            db.add_all([
                _artifact(
                    path="twitter/target_handle/new-101.json",
                    work_id="new-101",
                    creator_dir="target_handle",
                    job_id=historical.id,
                ),
                _artifact(
                    path="twitter/target_handle/expired-102.json",
                    work_id="expired-102",
                    creator_dir="target_handle",
                    job_id=historical.id,
                    state="importing",
                    import_job_id=stale_import.id,
                    lease_expires_at=now - timedelta(minutes=1),
                ),
                _artifact(
                    path="twitter/target_handle/live-103.json",
                    work_id="live-103",
                    creator_dir="target_handle",
                    job_id=historical.id,
                    state="importing",
                    import_job_id=live_import.id,
                    lease_expires_at=now + timedelta(minutes=10),
                ),
                _artifact(
                    path="twitter/target_handle/existing-104.json",
                    work_id="existing-104",
                    creator_dir="target_handle",
                    job_id=historical.id,
                ),
                _artifact(
                    path="twitter/other_handle/other-105.json",
                    work_id="other-105",
                    creator_dir="other_handle",
                    job_id=historical.id,
                ),
            ])
            await db.commit()

            reconciliation = await reconcile_repository_artifacts(db, current)
            await db.commit()

            assert reconciliation.downloaded_metadata_count == 0
            assert reconciliation.recovered_metadata_count == 2
            assert reconciliation.pending_work_count == 2
            assert reconciliation.downloaded_metadata_paths == ()
            assert set(reconciliation.recovered_metadata_paths) == {
                "twitter/target_handle/new-101.json",
                "twitter/target_handle/expired-102.json",
            }
            assert reconciliation.outcome_detail == {
                "downloaded_metadata_count": 0,
                "recovered_metadata_count": 2,
                "pending_work_count": 2,
            }

            rows = {
                row.source_work_id: row
                for row in (await db.execute(select(StorageArtifact))).scalars()
            }
            assert rows["new-101"].download_job_id == current.id
            assert rows["new-101"].state == "new"
            assert rows["expired-102"].download_job_id == current.id
            assert rows["expired-102"].state == "new"
            assert rows["existing-104"].state == "done"
            assert rows["live-103"].download_job_id == historical.id
            assert rows["live-103"].state == "importing"
            assert rows["live-103"].import_job_id == live_import.id
            assert rows["other-105"].download_job_id == historical.id
            assert other_repository.id != current.subscription_source_id

            repeated = await reconcile_repository_artifacts(db, current)
            assert repeated.downloaded_metadata_count == 2
            assert repeated.recovered_metadata_count == 0
            assert repeated.pending_work_count == 2
    finally:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repository_reconciliation_never_adopts_same_path_library_projection():
    """A library projection is not importable even when its relative path matches."""
    from app.database import async_session, engine
    from app.services.repository_artifact_reconciliation import (
        reconcile_repository_artifacts,
    )

    try:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
            _subscription, _repository, _other, current, historical, _manual = (
                await _repository_fixture(db)
            )
            relative_path = "twitter/target_handle/shared-identity.json"
            download_artifact = _artifact(
                path=relative_path,
                work_id="download-work",
                creator_dir="target_handle",
                job_id=historical.id,
            )
            library_projection = _artifact(
                path=relative_path,
                work_id="library-work",
                creator_dir="target_handle",
                job_id=historical.id,
            )
            library_projection.storage_root = "library"
            db.add_all([download_artifact, library_projection])
            await db.commit()

            result = await reconcile_repository_artifacts(db, current)
            await db.commit()

            assert result.recovered_metadata_paths == (relative_path,)
            assert result.pending_work_count == 1
            await db.refresh(download_artifact)
            await db.refresh(library_projection)
            assert download_artifact.download_job_id == current.id
            assert library_projection.download_job_id == historical.id
            assert library_projection.state == "new"
    finally:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repository_reconciliation_leaves_standalone_jobs_job_scoped():
    from app.database import async_session, engine
    from app.models import StorageArtifact
    from app.services.repository_artifact_reconciliation import (
        reconcile_repository_artifacts,
    )

    try:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
            _subscription, _repository, _other, _current, historical, manual = await _repository_fixture(db)
            backlog = _artifact(
                path="twitter/target_handle/manual-scope.json",
                work_id="manual-scope",
                creator_dir="target_handle",
                job_id=historical.id,
            )
            db.add(backlog)
            await db.commit()

            assert await reconcile_repository_artifacts(db, manual) is None
            await db.refresh(backlog)
            assert backlog.download_job_id == historical.id
            assert backlog.state == "new"
    finally:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recovered_backlog_is_retained_in_the_repository_receipt_outcome_detail():
    from app.database import async_session, engine
    from app.services.operation_attention import upsert_repository_sync_receipt
    from app.services.sync_outcome import build_sync_outcome

    try:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
            _subscription, repository, _other, current, _historical, _manual = await _repository_fixture(db)
            current.manifest = {
                "outcome": build_sync_outcome(
                    "new_content",
                    metadata_count=0,
                    media_count=0,
                    recovery_detail={
                        "downloaded_metadata_count": 0,
                        "recovered_metadata_count": 2,
                        "pending_work_count": 2,
                    },
                ),
            }
            current.status = "complete"
            await db.flush()
            await db.refresh(current)
            receipt = await upsert_repository_sync_receipt(db, current)
            await db.commit()

            assert receipt is not None
            assert receipt.repository_id == repository.id
            assert receipt.detail["outcome"] == {
                "code": "new_content",
                "metadata_count": 0,
                "media_count": 0,
                "downloaded_metadata_count": 0,
                "recovered_metadata_count": 2,
                "pending_work_count": 2,
                "completed_at": receipt.detail["outcome"]["completed_at"],
            }
    finally:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_successful_repository_sync_enqueues_recovered_backlog_when_network_delta_is_zero():
    from app.database import async_session, engine
    from app.jobs.download import _successful_repository_import_plan

    try:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
            _subscription, _repository, _other, current, historical, _manual = await _repository_fixture(db)
            db.add(_artifact(
                path="twitter/target_handle/recovered-201.json",
                work_id="recovered-201",
                creator_dir="target_handle",
                job_id=historical.id,
            ))
            await db.commit()

            pending_count, metadata_paths, reconciliation = await _successful_repository_import_plan(
                db,
                current,
                metadata_count=0,
                metadata_paths=[],
            )

            assert pending_count == 1
            assert metadata_paths == {"twitter/target_handle/recovered-201.json"}
            assert reconciliation is not None
            assert reconciliation.outcome_detail == {
                "downloaded_metadata_count": 0,
                "recovered_metadata_count": 1,
                "pending_work_count": 1,
            }
    finally:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_repository_identity_does_not_adopt_a_single_mismatched_source_creator():
    from app.database import async_session, engine
    from app.models import SourceCreator
    from app.services.repository_artifact_reconciliation import (
        reconcile_repository_artifacts,
    )
    from app.services.repository_identity import resolve_repository_source_creator_ids

    try:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
            subscription, repository, _other, current, historical, _manual = await _repository_fixture(
                db,
                source_creator_id=None,
            )
            db.add(SourceCreator(
                creator_id=subscription.creator_id,
                source="x",
                source_creator_id="other-numeric-id",
                source_url="https://x.com/other_handle",
            ))
            db.add(_artifact(
                path="twitter/other_handle/unrelated-301.json",
                work_id="unrelated-301",
                creator_dir="other_handle",
                job_id=historical.id,
            ))
            await db.commit()

            assert await resolve_repository_source_creator_ids(
                db, repository, subscription.creator_id,
            ) == []
            result = await reconcile_repository_artifacts(db, current)

            assert result.recovered_metadata_count == 0
            assert result.pending_work_count == 0
    finally:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "source_creator_id", "source_url", "directory"),
    [
        ("x", "123456", "https://x.com/target_handle", "target_handle"),
        ("iwara", "opaque-user-id", "https://www.iwara.tv/profile/target-name", "target-name"),
    ],
)
async def test_repository_reconciliation_uses_provider_directory_not_opaque_source_identity(
    source,
    source_creator_id,
    source_url,
    directory,
):
    from app.database import async_session, engine
    from app.services.repository_artifact_reconciliation import (
        _repository_creator_dirs,
        reconcile_repository_artifacts,
    )

    try:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
            subscription, repository, _other, current, historical, _manual = await _repository_fixture(
                db,
                source=source,
                source_creator_id=source_creator_id,
                source_url=source_url,
            )
            db.add_all([
                _artifact(
                    path=f"{source}/{source_creator_id}/opaque-401.json",
                    work_id="opaque-401",
                    creator_dir=source_creator_id,
                    job_id=historical.id,
                    source=source,
                ),
                _artifact(
                    path=f"{source}/{directory}/supported-402.json",
                    work_id="supported-402",
                    creator_dir=directory,
                    job_id=historical.id,
                    source=source,
                ),
            ])
            await db.commit()

            assert await _repository_creator_dirs(
                db, repository, subscription.creator_id,
            ) == {directory}
            result = await reconcile_repository_artifacts(db, current)
            await db.commit()

            assert set(result.recovered_metadata_paths) == {
                f"{source}/{directory}/supported-402.json",
            }
            assert result.recovered_metadata_count == 1
    finally:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_active_sibling_download_job_keeps_new_artifacts_after_adoption():
    from app.database import async_session, engine
    from app.models import DownloadJob, StorageArtifact
    from app.services.repository_artifact_reconciliation import (
        reconcile_repository_artifacts,
    )

    try:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
            subscription, repository, _other, first, historical, _manual = await _repository_fixture(db)
            second = DownloadJob(
                subscription_id=subscription.id,
                subscription_source_id=repository.id,
                source="x",
                source_url=repository.source_url,
                status="downloaded",
            )
            db.add(second)
            db.add(_artifact(
                path="twitter/target_handle/competing-501.json",
                work_id="competing-501",
                creator_dir="target_handle",
                job_id=historical.id,
            ))
            await db.commit()

            first_result = await reconcile_repository_artifacts(db, first)
            await db.commit()
            second_result = await reconcile_repository_artifacts(db, second)
            await db.commit()
            artifact = (await db.execute(
                select(StorageArtifact).where(
                    StorageArtifact.source_work_id == "competing-501",
                )
            )).scalar_one()

            assert first_result.recovered_metadata_count == 1
            assert second_result.recovered_metadata_count == 0
            assert artifact.download_job_id == first.id
            assert artifact.state == "new"
    finally:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner_status", "owner_is_orphan", "expect_recovered"),
    [
        ("complete", False, True),
        ("cancelled", False, True),
        ("failed", False, True),
        ("stale", False, True),
        ("future_state", False, False),
        ("complete", True, True),
    ],
)
async def test_repository_reconciliation_only_recovers_orphan_or_explicitly_recoverable_owners(
    owner_status,
    owner_is_orphan,
    expect_recovered,
):
    """Unknown DownloadJob statuses must fail closed rather than lose ownership."""
    from app.database import async_session, engine
    from app.models import StorageArtifact
    from app.services.repository_artifact_reconciliation import (
        reconcile_repository_artifacts,
    )

    try:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
            _subscription, _repository, _other, current, historical, _manual = await _repository_fixture(db)
            historical.status = owner_status
            db.add(_artifact(
                path="twitter/target_handle/owner-guard-701.json",
                work_id="owner-guard-701",
                creator_dir="target_handle",
                job_id=None if owner_is_orphan else historical.id,
            ))
            await db.commit()

            result = await reconcile_repository_artifacts(db, current)
            await db.commit()
            artifact = (await db.execute(
                select(StorageArtifact).where(
                    StorageArtifact.source_work_id == "owner-guard-701",
                )
            )).scalar_one()

            assert result.recovered_metadata_count == int(expect_recovered)
            assert artifact.download_job_id == (current.id if expect_recovered else historical.id)
    finally:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconciliation_serializes_owner_activation_before_backlog_adoption():
    """A terminal owner becoming active cannot race a repository adoption."""
    from app.database import async_session, engine
    from app.models import DownloadJob, StorageArtifact
    from app.services.repository_artifact_reconciliation import (
        reconcile_repository_artifacts,
    )

    try:
        async with async_session() as setup_db:
            await _clear_reconciliation_tables(setup_db)
            _subscription, _repository, _other, current, historical, _manual = await _repository_fixture(setup_db)
            db_current_id = current.id
            db_historical_id = historical.id
            artifact = _artifact(
                path="twitter/target_handle/owner-race-801.json",
                work_id="owner-race-801",
                creator_dir="target_handle",
                job_id=historical.id,
            )
            setup_db.add(artifact)
            await setup_db.commit()
            db_artifact_id = artifact.id

        async with async_session() as owner_db:
            owner = (await owner_db.execute(
                select(DownloadJob)
                .where(DownloadJob.id == db_historical_id)
                .with_for_update()
            )).scalar_one()
            assert owner.status == "complete"
            owner.status = "enqueued"
            await owner_db.flush()

            async def reconcile_in_second_session():
                async with async_session() as reconcile_db:
                    current_job = await reconcile_db.get(DownloadJob, db_current_id)
                    result = await reconcile_repository_artifacts(reconcile_db, current_job)
                    await reconcile_db.commit()
                    return result

            reconciliation_task = asyncio.create_task(reconcile_in_second_session())
            await asyncio.sleep(0.15)
            await owner_db.commit()
            result = await asyncio.wait_for(reconciliation_task, timeout=5)

        async with async_session() as verify_db:
            artifact = await verify_db.get(StorageArtifact, db_artifact_id)
            owner = await verify_db.get(DownloadJob, db_historical_id)
            assert owner.status == "enqueued"
            assert result.recovered_metadata_count == 0
            assert artifact.download_job_id == db_historical_id
    finally:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_compaction_and_reconciliation_share_artifact_then_owner_lock_order(monkeypatch):
    """A reset-to-new handoff cannot deadlock compaction against adoption."""
    from app.database import async_session, engine
    from app.models import DownloadJob, StorageArtifact
    from app.services import repository_artifact_reconciliation as reconciliation_service
    from app.services.operation_attention import (
        compact_terminal_tasks,
        upsert_repository_sync_receipt,
    )
    from app.services.tasks import TaskService

    artifact_rows_locked = asyncio.Event()
    allow_owner_lock = asyncio.Event()
    original_locked_owners = reconciliation_service._locked_recoverable_download_owner_ids

    async def gate_owner_lock(db, owner_ids):
        artifact_rows_locked.set()
        await asyncio.wait_for(allow_owner_lock.wait(), timeout=2)
        return await original_locked_owners(db, owner_ids)

    monkeypatch.setattr(
        reconciliation_service,
        "_locked_recoverable_download_owner_ids",
        gate_owner_lock,
    )
    try:
        async with async_session() as setup_db:
            await _clear_reconciliation_tables(setup_db)
            _subscription, _repository, _other, current, historical, _manual = await _repository_fixture(setup_db)
            task = await TaskService(setup_db).ensure_download_task(historical)
            task.compactable_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            artifact = _artifact(
                path="x/target_handle/lock-order-1001.json",
                work_id="lock-order-1001",
                creator_dir="target_handle",
                job_id=historical.id,
                state="done",
            )
            setup_db.add(artifact)
            await upsert_repository_sync_receipt(setup_db, historical)
            await setup_db.commit()
            current_id = current.id
            historical_id = historical.id
            artifact_id = artifact.id

        async def reconcile_in_first_session():
            async with async_session() as reconcile_db:
                current_job = await reconcile_db.get(DownloadJob, current_id)
                row = await reconcile_db.get(StorageArtifact, artifact_id, with_for_update=True)
                row.state = "new"
                await reconcile_db.flush()
                result = await reconciliation_service.reconcile_repository_artifacts(
                    reconcile_db,
                    current_job,
                )
                await reconcile_db.commit()
                return result

        reconciliation_task = asyncio.create_task(reconcile_in_first_session())
        await asyncio.wait_for(artifact_rows_locked.wait(), timeout=2)

        async def compact_in_second_session():
            async with async_session() as compactor_db:
                return await compact_terminal_tasks(compactor_db, dry_run=False)

        compaction_task = asyncio.create_task(compact_in_second_session())
        await asyncio.sleep(0.15)
        assert not compaction_task.done()
        allow_owner_lock.set()
        reconciliation, compaction = await asyncio.wait_for(
            asyncio.gather(reconciliation_task, compaction_task),
            timeout=3,
        )

        async with async_session() as verify_db:
            artifact = await verify_db.get(StorageArtifact, artifact_id)
            assert reconciliation.recovered_metadata_count == 1
            assert artifact.download_job_id == current_id
            assert artifact.state == "new"
            assert await verify_db.get(DownloadJob, historical_id) is None
            assert compaction["deleted_download_jobs"] == 1
    finally:
        allow_owner_lock.set()
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconciliation_mutates_only_the_exact_unlocked_duplicate_artifacts():
    """SKIP LOCKED rows with duplicate identities remain outside this run."""
    from app.database import async_session, engine
    from app.models import DownloadJob, StorageArtifact, Work, WorkSource
    from app.services.repository_artifact_reconciliation import (
        reconcile_repository_artifacts,
    )

    try:
        async with async_session() as setup_db:
            await _clear_reconciliation_tables(setup_db)
            _subscription, _repository, _other, current, historical, _manual = await _repository_fixture(setup_db)
            current_id = current.id
            historical_id = historical.id
            existing_work = Work(title="already imported duplicate")
            setup_db.add(existing_work)
            await setup_db.flush()
            setup_db.add(WorkSource(
                work_id=existing_work.id,
                source="x",
                source_work_id="existing-902",
            ))
            selected_new = _artifact(
                path="twitter/target_handle/new-selected-901.json",
                work_id="new-901",
                creator_dir="target_handle",
                job_id=historical.id,
            )
            selected_existing = _artifact(
                path="twitter/target_handle/existing-selected-902.json",
                work_id="existing-902",
                creator_dir="target_handle",
                job_id=historical.id,
            )
            locked_new = _artifact(
                path="twitter/target_handle/new-locked-901.json",
                work_id="new-901",
                creator_dir="target_handle",
                job_id=historical.id,
            )
            locked_existing = _artifact(
                path="twitter/target_handle/existing-locked-902.json",
                work_id="existing-902",
                creator_dir="target_handle",
                job_id=historical.id,
            )
            setup_db.add_all([selected_new, selected_existing, locked_new, locked_existing])
            await setup_db.commit()
            selected_new_id = selected_new.id
            selected_existing_id = selected_existing.id
            locked_ids = {locked_new.id, locked_existing.id}

        async with async_session() as lock_db:
            locked_rows = list((await lock_db.execute(
                select(StorageArtifact)
                .where(StorageArtifact.id.in_(locked_ids))
                .order_by(StorageArtifact.id)
                .with_for_update()
            )).scalars())
            assert {row.id for row in locked_rows} == locked_ids

            async def reconcile_in_second_session():
                async with async_session() as reconcile_db:
                    current_job = await reconcile_db.get(DownloadJob, current_id)
                    result = await reconcile_repository_artifacts(reconcile_db, current_job)
                    await reconcile_db.commit()
                    return result

            reconciliation_task = asyncio.create_task(reconcile_in_second_session())
            try:
                result = await asyncio.wait_for(reconciliation_task, timeout=2)
            finally:
                await lock_db.commit()

        async with async_session() as verify_db:
            rows = {
                row.id: row
                for row in (await verify_db.execute(select(StorageArtifact))).scalars()
            }
            assert result.recovered_metadata_paths == (
                "twitter/target_handle/new-selected-901.json",
            )
            assert result.recovered_metadata_count == 1
            assert result.pending_work_count == 1
            assert rows[selected_new_id].download_job_id == current_id
            assert rows[selected_new_id].state == "new"
            assert rows[selected_existing_id].download_job_id == historical_id
            assert rows[selected_existing_id].state == "done"
            for locked_id in locked_ids:
                assert rows[locked_id].download_job_id == historical_id
                assert rows[locked_id].state == "new"
    finally:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_successful_download_boundary_retries_durable_recovered_backlog_after_enqueue_crash(
    tmp_path,
    monkeypatch,
):
    """A crash after ledger adoption is re-enqueued by import recovery."""
    from app.database import async_session, engine
    from app.jobs import download as download_module
    from app.models import DownloadJob, ImportJob
    from app.services.import_recovery import recover_import_pipeline
    from app.services import job_progress

    class FakeProcess:
        pid = 987654
        returncode = 0

        def __init__(self, *_args, **_kwargs):
            self.stdout = StringIO("")
            self.stderr = StringIO("")

        def poll(self):
            return self.returncode

        def wait(self, _timeout=None):
            return self.returncode

    class FakeControlListener:
        command = None

        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def detach_process(self, _expected_pid):
            return True

    class FakeHeartbeat(FakeControlListener):
        def transfer_to_pid(self, _pid):
            return True

    initial_publications: list[set[str]] = []
    recovery_publications: list[set[str]] = []

    async def fake_defaults():
        return {"max_posts": 1, "timeout_seconds": 1, "stall_timeout_seconds": 1}

    async def fake_enqueue(_job_id, import_error=None, new_json_paths=None):
        paths = set(new_json_paths or [])
        if import_error is None:
            assert initial_publications == []
            initial_publications.append(paths)
            raise RuntimeError("simulated crash after durable reconciliation")
        assert import_error == "auto recovery after downloaded state gap (1 metadata files)"
        assert recovery_publications == []
        recovery_publications.append(paths)
        async with async_session() as enqueue_db:
            recovery_job = await enqueue_db.get(DownloadJob, UUID(_job_id))
            assert recovery_job is not None
            recovery_job.status = "importing"
            enqueue_db.add(ImportJob(
                download_job_id=recovery_job.id,
                status="enqueued",
            ))
            await enqueue_db.commit()
        return "durable-import-id"

    raw_runner = download_module.run_download_job
    while hasattr(raw_runner, "__wrapped__"):
        raw_runner = raw_runner.__wrapped__
    monkeypatch.setattr(download_module, "_read_download_defaults", fake_defaults)
    monkeypatch.setattr(download_module, "_enqueue_import", fake_enqueue)
    monkeypatch.setattr(download_module, "build_effective_gallerydl_config", lambda *_args: {})
    monkeypatch.setattr(download_module, "staging_enabled", lambda: False)
    monkeypatch.setattr(download_module, "ControlListener", FakeControlListener)
    monkeypatch.setattr(download_module, "HeartbeatPublisher", FakeHeartbeat)
    monkeypatch.setattr(download_module.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(download_module, "_process_group_exists", lambda _pid: False)
    monkeypatch.setattr(download_module.settings, "download_root", str(tmp_path))
    monkeypatch.setattr(job_progress.ProgressTracker, "set", staticmethod(lambda *_args: None))
    monkeypatch.setattr(
        job_progress.TaskEventPublisher,
        "publish_progress",
        staticmethod(lambda *_args: None),
    )
    monkeypatch.setattr(download_module, "get_redis", lambda: SimpleNamespace(
        hset=lambda *_args, **_kwargs: None,
        expire=lambda *_args, **_kwargs: None,
    ))

    try:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
            _subscription, _repository, _other, current, historical, _manual = await _repository_fixture(db)
            current.status = "enqueued"
            db.add(_artifact(
                path="twitter/target_handle/retry-601.json",
                work_id="retry-601",
                creator_dir="target_handle",
                job_id=historical.id,
            ))
            await db.commit()

        with pytest.raises(RuntimeError, match="simulated crash"):
            await raw_runner(str(current.id))

        async with async_session() as db:
            refreshed = await db.get(type(current), current.id)
            assert refreshed is not None
            assert refreshed.status == "downloaded"
            assert refreshed.manifest["repository_artifact_reconciliation"] == {
                "downloaded_metadata_count": 0,
                "recovered_metadata_count": 1,
                "pending_work_count": 1,
            }
            # The recovery service deliberately waits for a grace period before
            # it fills a missing import publication.  Simulate that elapsed
            # interval without altering the durable downloaded state.
            refreshed.updated_at = datetime.now(timezone.utc) - timedelta(minutes=2)
            await db.commit()

        async with async_session() as db:
            recovery = await recover_import_pipeline(
                db,
                stale_after_seconds=1,
            )
        async with async_session() as db:
            repeated_recovery = await recover_import_pipeline(
                db,
                stale_after_seconds=1,
            )

        assert initial_publications == [{"twitter/target_handle/retry-601.json"}]
        assert recovery_publications == [{"twitter/target_handle/retry-601.json"}]
        assert recovery["imports_enqueued"] == 1
        assert recovery["download_job_ids"] == [str(current.id)]
        assert repeated_recovery["imports_enqueued"] == 0
        assert repeated_recovery["download_job_ids"] == []
    finally:
        async with async_session() as db:
            await _clear_reconciliation_tables(db)
        await engine.dispose()
