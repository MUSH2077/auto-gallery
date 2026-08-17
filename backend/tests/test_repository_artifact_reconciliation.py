from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


async def _repository_fixture(db):
    from app.models import Creator, DownloadJob, Subscription, SubscriptionSource

    creator = Creator(name="reconcile-creator", display_name="Reconcile Creator")
    db.add(creator)
    await db.flush()
    subscription = Subscription(creator_id=creator.id, name="Reconcile Creator")
    db.add(subscription)
    await db.flush()
    repository = SubscriptionSource(
        subscription_id=subscription.id,
        source="x",
        source_creator_id="opaque-target-id",
        source_url="https://x.com/target_handle",
    )
    other_repository = SubscriptionSource(
        subscription_id=subscription.id,
        source="x",
        source_creator_id="opaque-other-id",
        source_url="https://x.com/other_handle",
    )
    db.add_all([repository, other_repository])
    await db.flush()
    current = DownloadJob(
        subscription_id=subscription.id,
        subscription_source_id=repository.id,
        source="x",
        source_url=repository.source_url,
        status="downloaded",
    )
    historical = DownloadJob(
        subscription_id=subscription.id,
        subscription_source_id=repository.id,
        source="x",
        source_url=repository.source_url,
        status="complete",
    )
    manual = DownloadJob(
        subscription_id=subscription.id,
        source="x",
        source_url="https://x.com/manual_handle",
        status="downloaded",
    )
    db.add_all([current, historical, manual])
    await db.flush()
    return subscription, repository, other_repository, current, historical, manual


def _artifact(*, path: str, work_id: str, creator_dir: str, job_id, state: str = "new", **extra):
    from app.models import StorageArtifact

    return StorageArtifact(
        storage_root="downloads",
        file_path=path,
        source="x",
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
