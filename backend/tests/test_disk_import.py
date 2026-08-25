import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update


_PUBLISHER_ATTEMPT_META_KEY = "_bounded_import_publisher_attempt"


def test_reconcile_downloads_to_db_is_importable():
    # Contract smoke test: the service module exposes the entrypoint with the
    # documented signature. Behavioural coverage is via integration runs.
    from app.services.disk_import import reconcile_downloads_to_db
    import inspect

    sig = inspect.signature(reconcile_downloads_to_db)
    assert list(sig.parameters)[:2] == ["db", "options"]


def test_import_from_disk_request_preserves_optional_repository_scope():
    """The enqueue request carries a typed repository scope to the worker."""
    from uuid import uuid4

    from app.api.admin.data import ImportFromDiskRequest

    repository_id = uuid4()
    request = ImportFromDiskRequest(source="pixiv", repository_id=repository_id)

    assert request.repository_id == repository_id
    assert request.model_dump(mode="json")["repository_id"] == str(repository_id)


async def _clear_pipeline_tables(db):
    await db.execute(text("""
        TRUNCATE
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
            creator_links,
            source_creators,
            subscription_sources,
            subscriptions,
            creators
        RESTART IDENTITY CASCADE
    """))
    await db.commit()


def _pixiv_metadata(work_id: int = 38362603, creator_id: int = 1980643) -> dict:
    return {
        "id": work_id,
        "title": "Fixture illustration",
        "description": "Synthetic disk import fixture",
        "date": "2024-01-01 12:00:00",
        "user": {"id": creator_id, "name": "Fixture Artist", "account": "fixture_artist"},
        "tags": [{"name": "fixture"}],
        "pages": [{"url": "https://example.test/img.jpg", "width": 16, "height": 16}],
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_downloads_to_db_registers_and_enqueues_idempotently(tmp_path, monkeypatch):
    from app.config import settings
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.download_job import DownloadJob
    from app.models.storage_artifact import StorageArtifact
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.services.disk_import import reconcile_downloads_to_db

    download_root = tmp_path / "downloads"
    work_dir = download_root / "pixiv" / "1980643" / "38362603"
    work_dir.mkdir(parents=True)
    (work_dir / "metadata.json").write_text(json.dumps(_pixiv_metadata()), encoding="utf-8")
    (work_dir / "p0.jpg").write_bytes(b"not-a-real-jpeg-but-good-enough-for-ledger")
    monkeypatch.setattr(settings, "download_root", str(download_root))
    monkeypatch.setattr("app.services.disk_identity.danbooru_svc.search_and_extract", lambda **_: (None, []))

    enqueued: list[tuple[str, set[str] | None]] = []

    async def fake_enqueue(download_job_id: str, import_error: str | None = None, new_json_paths=None):
        enqueued.append((download_job_id, set(new_json_paths or [])))
        return "fake-import-job"

    monkeypatch.setattr("app.jobs.download._enqueue_import", fake_enqueue)

    try:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
            creator = Creator(name="fixture_artist", display_name="Fixture Artist")
            db.add(creator)
            await db.flush()
            sub = Subscription(creator_id=creator.id, name="Fixture Artist", sync_enabled=True, schedule_mode="interval")
            db.add(sub)
            await db.flush()
            db.add(SubscriptionSource(
                subscription_id=sub.id,
                source="pixiv",
                source_creator_id="1980643",
                source_url="https://www.pixiv.net/users/1980643",
                is_enabled=True,
            ))
            await db.commit()

            result = await reconcile_downloads_to_db(
                db,
                {"source": "pixiv", "reset_ledger": True},
            )

            assert {k: result[k] for k in ("sources", "creators", "jobs", "skipped_done")} == {"sources": 1, "creators": 1, "jobs": 1, "skipped_done": 0}
            assert result["import_job_ids"] == ["fake-import-job"]
            assert len(enqueued) == 1
            assert enqueued[0][1] == {str(work_dir / "metadata.json")}
            assert sub.sync_enabled is True
            assert sub.schedule_mode == "interval"
            assert (await db.execute(select(func.count(DownloadJob.id)))).scalar_one() == 1
            ss = (await db.execute(select(SubscriptionSource))).scalar_one()
            assert ss.is_enabled is True
            assert (await db.execute(select(func.count(StorageArtifact.id)))).scalar_one() == 2

            original_job_id = (await db.execute(select(DownloadJob.id))).scalar_one()
            library_artifact = StorageArtifact(
                storage_root="library",
                file_path="pixiv/1980643/38362603/metadata.json",
                source="pixiv",
                creator_dir="1980643",
                source_work_id="38362603",
                file_name="metadata.json",
                artifact_type="metadata_json",
                download_job_id=original_job_id,
                state="done",
            )
            db.add(library_artifact)

            await db.execute(update(StorageArtifact).values(state="done"))
            await db.commit()

            second = await reconcile_downloads_to_db(db, {"source": "pixiv"})

            assert {k: second[k] for k in ("sources", "creators", "jobs", "skipped_done")} == {"sources": 0, "creators": 0, "jobs": 0, "skipped_done": 0}
            assert second["import_job_ids"] == []
            assert len(enqueued) == 1
            assert (await db.execute(select(func.count(DownloadJob.id)))).scalar_one() == 1

            # reset_ledger recovery: reprocess even 'done' files. This is the
            # path for recovering creators deleted from the DB while the ledger
            # still marks their files 'done'.
            third = await reconcile_downloads_to_db(db, {"source": "pixiv", "reset_ledger": True})
            assert third["reset_ledger"] is True
            assert third["skipped_done"] == 0
            assert third["creators"] == 1
            assert third["jobs"] == 1
            assert len(enqueued) == 2

            recovered_job = (
                await db.execute(
                    select(DownloadJob).order_by(
                        DownloadJob.created_at.desc(),
                        DownloadJob.id.desc(),
                    ).limit(1)
                )
            ).scalar_one()
            await db.refresh(library_artifact)
            assert library_artifact.download_job_id == original_job_id
            assert library_artifact.state == "done"
            from uuid import uuid4
            from app.models.import_job import ImportJob
            from app.services.artifact_ledger import ArtifactLedger

            claimant = ImportJob(
                download_job_id=recovered_job.id,
                status="running",
                execution_token=uuid4(),
            )
            db.add(claimant)
            await db.flush()
            claimed = await ArtifactLedger(db).claim_work_batch(
                recovered_job.id,
                claimant.id,
                lease_token=claimant.execution_token,
                limit=25,
            )
            assert claimed.claimed == ("38362603",)
    finally:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disk_import_repository_scope_uses_exact_creator_directory_and_progress(tmp_path, monkeypatch):
    """A repository drain must never absorb another same-provider creator."""
    from app.config import settings
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.services.disk_import import reconcile_downloads_to_db

    download_root = tmp_path / "downloads"
    selected_dir = download_root / "pixiv" / "1980643" / "38362603"
    other_dir = download_root / "pixiv" / "999999" / "38362604"
    selected_dir.mkdir(parents=True)
    other_dir.mkdir(parents=True)
    (selected_dir / "metadata.json").write_text(json.dumps(_pixiv_metadata()), encoding="utf-8")
    (selected_dir / "p0.jpg").write_bytes(b"fixture")
    (other_dir / "metadata.json").write_text(
        json.dumps(_pixiv_metadata(work_id=38362604, creator_id=999999)),
        encoding="utf-8",
    )
    (other_dir / "p0.jpg").write_bytes(b"fixture")
    monkeypatch.setattr(settings, "download_root", str(download_root))
    monkeypatch.setattr("app.services.disk_identity.danbooru_svc.search_and_extract", lambda **_: (None, []))

    enqueued: list[set[str]] = []

    async def fake_enqueue(_download_job_id, import_error=None, new_json_paths=None):
        enqueued.append(set(new_json_paths or []))
        return f"import-{len(enqueued)}"

    monkeypatch.setattr("app.jobs.download._enqueue_import", fake_enqueue)
    progress_reports: list[dict] = []

    async def record_progress(progress: dict):
        progress_reports.append(progress)

    try:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
            creator = Creator(name="selected")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="Selected")
            db.add(subscription)
            await db.flush()
            repository = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id="1980643",
                source_url="https://www.pixiv.net/users/1980643",
            )
            db.add(repository)
            await db.commit()

            result = await reconcile_downloads_to_db(
                db,
                {
                    "source": "pixiv",
                    "repository_id": str(repository.id),
                    "reset_ledger": True,
                },
                progress_callback=record_progress,
            )

            assert result["scanned"] == 1
            assert result["imported"] == 1
            assert result["existing"] == 0
            assert result["skipped"] == 0
            assert result["failed"] == 0
            assert result["jobs"] == 1
            assert enqueued == [{str(selected_dir / "metadata.json")}]
            assert progress_reports[-1] == {
                "phase": "running",
                "scanned": 1,
                "total": 1,
                "source": "pixiv",
                "existing": 0,
                "imported": 1,
                "skipped": 0,
                "failed": 0,
            }
    finally:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repository_drain_resolves_legacy_username_directory_from_pending_metadata(
    tmp_path,
    monkeypatch,
):
    """A UID repository adopts its old gallery-dl username directory only."""
    from app.config import settings
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.storage_artifact import StorageArtifact
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.services.disk_import import reconcile_downloads_to_db

    download_root = tmp_path / "downloads"
    selected_dir = download_root / "pixiv" / "legacy_account" / "38362603"
    sibling_dir = download_root / "pixiv" / "other_account" / "38362604"
    selected_dir.mkdir(parents=True)
    sibling_dir.mkdir(parents=True)
    selected_json = selected_dir / "metadata.json"
    selected_json.write_text(json.dumps(_pixiv_metadata()), encoding="utf-8")
    sibling_json = sibling_dir / "metadata.json"
    sibling_json.write_text(
        json.dumps(_pixiv_metadata(work_id=38362604, creator_id=999999)),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "download_root", str(download_root))

    enqueued: list[set[str]] = []

    async def fake_enqueue(_download_job_id, import_error=None, new_json_paths=None):
        enqueued.append(set(new_json_paths or []))
        return "legacy-scoped-import"

    monkeypatch.setattr("app.jobs.download._enqueue_import", fake_enqueue)

    try:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
            creator = Creator(name="selected")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="Selected")
            db.add(subscription)
            await db.flush()
            repository = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id="1980643",
                source_url="https://www.pixiv.net/users/1980643",
            )
            db.add(repository)
            await db.flush()
            db.add_all([
                StorageArtifact(
                    storage_root="downloads",
                    file_path="pixiv/legacy_account/38362603/metadata.json",
                    source="pixiv",
                    creator_dir="legacy_account",
                    source_work_id="38362603",
                    file_name="metadata.json",
                    artifact_type="metadata_json",
                    state="new",
                ),
                StorageArtifact(
                    storage_root="downloads",
                    file_path="pixiv/other_account/38362604/metadata.json",
                    source="pixiv",
                    creator_dir="other_account",
                    source_work_id="38362604",
                    file_name="metadata.json",
                    artifact_type="metadata_json",
                    state="new",
                ),
            ])
            await db.commit()

            result = await reconcile_downloads_to_db(
                db,
                {"source": "pixiv", "repository_id": str(repository.id)},
            )

            assert result["scanned"] == 1
            assert result["imported"] == 1
            assert result["failed"] == 0
            assert enqueued == [{str(selected_json)}]
    finally:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ordinary_repository_drain_never_walks_sibling_directories(tmp_path, monkeypatch):
    """A scoped ledger drain must not discover any sibling creator on disk."""
    from pathlib import Path

    from app.config import settings
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.download_job import DownloadJob
    from app.models.storage_artifact import StorageArtifact
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.services.disk_import import reconcile_downloads_to_db

    download_root = tmp_path / "downloads"
    selected_dir = download_root / "pixiv" / "1980643" / "38362603"
    sibling_dir = download_root / "pixiv" / "999999" / "38362604"
    selected_dir.mkdir(parents=True)
    sibling_dir.mkdir(parents=True)
    selected_json = selected_dir / "metadata.json"
    selected_json.write_text(json.dumps(_pixiv_metadata()), encoding="utf-8")
    (sibling_dir / "metadata.json").write_text("not selected", encoding="utf-8")
    monkeypatch.setattr(settings, "download_root", str(download_root))

    original_rglob = Path.rglob

    def reject_source_tree_walk(path, pattern):
        if path == download_root / "pixiv":
            raise AssertionError("ordinary repository drain walked the source tree")
        return original_rglob(path, pattern)

    monkeypatch.setattr(Path, "rglob", reject_source_tree_walk)
    enqueued = []

    async def fake_enqueue(_download_job_id, import_error=None, new_json_paths=None):
        enqueued.append(set(new_json_paths or []))
        return "scoped-ledger-import"

    monkeypatch.setattr("app.jobs.download._enqueue_import", fake_enqueue)

    try:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
            creator = Creator(name="selected")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="Selected")
            db.add(subscription)
            await db.flush()
            repository = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id="1980643",
                source_url="https://www.pixiv.net/users/1980643",
            )
            db.add(repository)
            await db.flush()
            historical = DownloadJob(
                subscription_id=subscription.id,
                subscription_source_id=repository.id,
                source="pixiv",
                source_url=repository.source_url,
                status="complete",
            )
            db.add(historical)
            await db.flush()
            other_creator = Creator(name="other-owner")
            db.add(other_creator)
            await db.flush()
            other_subscription = Subscription(
                creator_id=other_creator.id,
                name="Other owner",
            )
            db.add(other_subscription)
            await db.flush()
            other_repository = SubscriptionSource(
                subscription_id=other_subscription.id,
                source="pixiv",
                source_creator_id="1980643",
                source_url="https://www.pixiv.net/users/999999",
            )
            db.add(other_repository)
            await db.flush()
            other_historical = DownloadJob(
                subscription_id=other_subscription.id,
                subscription_source_id=other_repository.id,
                source="pixiv",
                source_url=other_repository.source_url,
                status="complete",
            )
            db.add(other_historical)
            await db.flush()
            db.add(StorageArtifact(
                storage_root="downloads",
                file_path="pixiv/1980643/38362603/metadata.json",
                source="pixiv",
                creator_dir="1980643",
                source_work_id="38362603",
                file_name="metadata.json",
                artifact_type="metadata_json",
                download_job_id=historical.id,
                state="new",
            ))
            db.add(StorageArtifact(
                storage_root="downloads",
                file_path="pixiv/1980643/38362699/metadata.json",
                source="pixiv",
                creator_dir="1980643",
                source_work_id="38362699",
                file_name="metadata.json",
                artifact_type="metadata_json",
                download_job_id=other_historical.id,
                state="new",
            ))
            other_work_dir = download_root / "pixiv" / "1980643" / "38362699"
            other_work_dir.mkdir()
            (other_work_dir / "metadata.json").write_text(
                json.dumps(_pixiv_metadata(work_id=38362699)),
                encoding="utf-8",
            )
            await db.commit()

            result = await reconcile_downloads_to_db(
                db,
                {"source": "pixiv", "repository_id": str(repository.id)},
            )

            assert result["imported"] == 1
            assert enqueued == [{str(selected_json)}]
    finally:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ordinary_disk_drain_keyset_feeds_25_work_batches_with_backpressure(tmp_path, monkeypatch):
    """A large ledger scope advances 25/25/1 without touching library twins."""
    from app.config import settings
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.download_job import DownloadJob
    from app.models.storage_artifact import StorageArtifact
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.services import disk_import as disk_import_service

    download_root = tmp_path / "downloads"
    creator_root = download_root / "pixiv" / "1980643"
    creator_root.mkdir(parents=True)
    monkeypatch.setattr(settings, "download_root", str(download_root))

    enqueued: list[list[str]] = []
    capacity_checks: list[str | None] = []
    external_transaction_states: list[tuple[str, bool]] = []

    async def fake_enqueue(
        _download_job_id,
        import_error=None,
        new_json_paths=None,
        publisher_checkpoint=None,
    ):
        del import_error, publisher_checkpoint
        external_transaction_states.append(("enqueue", db.in_transaction()))
        enqueued.append(sorted(str(path) for path in (new_json_paths or [])))
        return f"batch-import-{len(enqueued)}"

    async def record_capacity(parent_task_id):
        external_transaction_states.append(("capacity", db.in_transaction()))
        capacity_checks.append(parent_task_id)

    async def fence_transaction(session, *, lock_task=False):
        del lock_task
        await session.execute(text("SELECT 1"))

    monkeypatch.setattr("app.jobs.download._enqueue_import", fake_enqueue)
    monkeypatch.setattr(
        disk_import_service,
        "_wait_for_batch_capacity",
        record_capacity,
        raising=False,
    )

    try:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
            creator = Creator(name="batch-owner")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="Batch owner")
            db.add(subscription)
            await db.flush()
            repository = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id="1980643",
                source_url="https://www.pixiv.net/users/1980643",
            )
            db.add(repository)
            await db.flush()
            historical = DownloadJob(
                subscription_id=subscription.id,
                subscription_source_id=repository.id,
                source="pixiv",
                source_url=repository.source_url,
                status="complete",
            )
            db.add(historical)
            await db.flush()

            artifacts = []
            for index in range(1, 52):
                work_id = f"{index:03d}"
                work_dir = creator_root / work_id
                work_dir.mkdir()
                metadata_path = work_dir / "metadata.json"
                metadata_path.write_text(
                    json.dumps(_pixiv_metadata(work_id=index)),
                    encoding="utf-8",
                )
                artifacts.append(StorageArtifact(
                    storage_root="downloads",
                    file_path=f"pixiv/1980643/{work_id}/metadata.json",
                    source="pixiv",
                    creator_dir="1980643",
                    source_work_id=work_id,
                    file_name="metadata.json",
                    artifact_type="metadata_json",
                    download_job_id=historical.id,
                    state="new",
                ))
            library_twin = StorageArtifact(
                storage_root="library",
                file_path="pixiv/1980643/001/metadata.json",
                source="pixiv",
                creator_dir="1980643",
                source_work_id="library-only",
                file_name="metadata.json",
                artifact_type="metadata_json",
                download_job_id=historical.id,
                state="done",
            )
            db.add_all([*artifacts, library_twin])
            await db.commit()

            result = await disk_import_service.reconcile_downloads_to_db(
                db,
                {
                    "source": "pixiv",
                    "parent_task_id": "parent-drain",
                },
                publisher_checkpoint=fence_transaction,
            )

            assert [len(batch) for batch in enqueued] == [25, 25, 1]
            assert len({path for batch in enqueued for path in batch}) == 51
            assert capacity_checks == ["parent-drain"] * 3
            assert external_transaction_states == [
                ("capacity", False),
                ("enqueue", False),
            ] * 3
            assert result["imported"] == 3
            assert result["scanned"] == 51
            await db.refresh(library_twin)
            assert library_twin.download_job_id == historical.id
            assert library_twin.state == "done"
    finally:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_publication_assigns_only_its_bounded_metadata_feed(tmp_path, monkeypatch):
    """Concurrent child imports must each read only their durable path assignment."""
    from uuid import UUID, uuid4

    from app.config import settings
    from app.database import async_session, engine
    from app.jobs.download import _enqueue_import
    from app.models.creator import Creator
    from app.models.download_job import DownloadJob
    from app.models.import_job import ImportJob
    from app.models.storage_artifact import StorageArtifact
    from app.models.subscription import Subscription
    from app.services.artifact_ledger import ArtifactLedger

    download_root = tmp_path / "downloads"
    first_path = download_root / "pixiv" / "bounded" / "001" / "metadata.json"
    second_path = download_root / "pixiv" / "bounded" / "002" / "metadata.json"
    first_path.parent.mkdir(parents=True)
    second_path.parent.mkdir(parents=True)
    first_path.write_text(json.dumps(_pixiv_metadata(work_id=1)), encoding="utf-8")
    second_path.write_text(json.dumps(_pixiv_metadata(work_id=2)), encoding="utf-8")
    monkeypatch.setattr(settings, "download_root", str(download_root))

    class FakeRedis:
        def setex(self, *_args, **_kwargs):
            return True

    async def fake_publish(*_args, **_kwargs):
        return "published"

    monkeypatch.setattr("app.jobs.download.get_redis", lambda: FakeRedis())
    monkeypatch.setattr("app.jobs.download.publish_prepared_import", fake_publish)
    monkeypatch.setattr("app.jobs.download.publish_progress", lambda *_args, **_kwargs: None)

    try:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
            creator = Creator(name="bounded-publication")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="Bounded publication")
            db.add(subscription)
            await db.flush()
            download = DownloadJob(
                subscription_id=subscription.id,
                source="pixiv",
                source_url="https://www.pixiv.net/users/1980643",
                status="downloaded",
            )
            db.add(download)
            await db.flush()
            first = StorageArtifact(
                storage_root="downloads",
                file_path="pixiv/bounded/001/metadata.json",
                source="pixiv",
                creator_dir="bounded",
                source_work_id="001",
                file_name="metadata.json",
                artifact_type="metadata_json",
                download_job_id=download.id,
                state="new",
            )
            second = StorageArtifact(
                storage_root="downloads",
                file_path="pixiv/bounded/002/metadata.json",
                source="pixiv",
                creator_dir="bounded",
                source_work_id="002",
                file_name="metadata.json",
                artifact_type="metadata_json",
                download_job_id=download.id,
                state="new",
            )
            library_projection = StorageArtifact(
                storage_root="library",
                file_path="pixiv/bounded/001/metadata.json",
                source="pixiv",
                creator_dir="bounded",
                source_work_id="001",
                file_name="metadata.json",
                artifact_type="metadata_json",
                download_job_id=download.id,
                state="new",
            )
            db.add_all([first, second, library_projection])
            await db.commit()
            download_id = download.id
            first_id = first.id
            second_id = second.id
            library_projection_id = library_projection.id

        import_job_id = UUID(await _enqueue_import(
            str(download_id),
            new_json_paths={str(first_path)},
        ))

        async with async_session() as db:
            first_row = await db.get(StorageArtifact, first_id)
            second_row = await db.get(StorageArtifact, second_id)
            assert first_row.import_job_id == import_job_id
            assert second_row.import_job_id is None
            assert await ArtifactLedger(db).new_metadata_paths(
                download_id,
                import_job_id=import_job_id,
            ) == ["pixiv/bounded/001/metadata.json"]
            import_job = await db.get(ImportJob, import_job_id)
            import_job.status = "running"
            import_job.execution_token = uuid4()
            await db.flush()
            claim = await ArtifactLedger(db).claim_work_batch(
                download_id,
                import_job_id,
                lease_token=import_job.execution_token,
                source="pixiv",
                source_work_ids=["001"],
            )
            await db.commit()
            assert claim.claimed == ("001",)
            library_row = await db.get(StorageArtifact, library_projection_id)
            assert library_row.state == "new"
            assert library_row.import_job_id is None
            assert library_row.lease_token is None
    finally:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disk_import_rejects_repository_source_mismatch(tmp_path, monkeypatch):
    """The scope token is only valid for the repository's canonical source."""
    from app.config import settings
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.services.disk_import import reconcile_downloads_to_db

    monkeypatch.setattr(settings, "download_root", str(tmp_path / "downloads"))
    try:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
            creator = Creator(name="source-mismatch")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="Source mismatch")
            db.add(subscription)
            await db.flush()
            repository = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id="1980643",
                source_url="https://www.pixiv.net/users/1980643",
            )
            db.add(repository)
            await db.commit()

            with pytest.raises(ValueError, match="does not match repository source"):
                await reconcile_downloads_to_db(
                    db,
                    {"source": "x", "repository_id": str(repository.id)},
                )
    finally:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disk_import_records_enqueue_failure_and_leaves_ledger_resumable(tmp_path, monkeypatch):
    """One creator failure must not erase the durable ledger or abort the drain."""
    from app.config import settings
    from app.database import async_session, engine
    from app.models.storage_artifact import StorageArtifact
    from app.services.disk_import import reconcile_downloads_to_db

    download_root = tmp_path / "downloads"
    work_dir = download_root / "pixiv" / "1980643" / "38362603"
    work_dir.mkdir(parents=True)
    (work_dir / "metadata.json").write_text(json.dumps(_pixiv_metadata()), encoding="utf-8")
    (work_dir / "p0.jpg").write_bytes(b"fixture")
    monkeypatch.setattr(settings, "download_root", str(download_root))
    monkeypatch.setattr("app.services.disk_identity.danbooru_svc.search_and_extract", lambda **_: (None, []))

    async def failing_enqueue(*_args, **_kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr("app.jobs.download._enqueue_import", failing_enqueue)

    try:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
            result = await reconcile_downloads_to_db(
                db,
                {"source": "pixiv", "reset_ledger": True},
            )

            assert result["scanned"] == 1
            assert result["imported"] == 0
            assert result["failed"] == 1
            assert result["jobs"] == 0
            assert int((await db.execute(select(func.count(StorageArtifact.id)))).scalar_one()) == 2
            states = set((await db.execute(select(StorageArtifact.state))).scalars())
            assert states == {"new"}
    finally:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_bounded_enqueue_failure_cannot_fail_parent_or_commit_progress(
    tmp_path,
    monkeypatch,
):
    """Authority rotation after the last checkpoint defeats the failure commit."""
    from app.config import settings
    from app.database import async_session, engine
    from app.jobs.admin_operations import _DiskImportPublisherGuard
    from app.models.creator import Creator
    from app.models.download_job import DownloadJob
    from app.models.storage_artifact import StorageArtifact
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.models.task_run import TaskRun
    from app.services import disk_import as disk_import_service
    from app.services.redis_client import get_redis
    from app.services.redis_pubsub import PublisherFenceError
    from app.services.tasks import TaskService

    task_id = uuid4()
    attempt_a = uuid4().hex
    attempt_b = uuid4().hex
    enqueue_entered = asyncio.Event()
    release_enqueue = asyncio.Event()
    download_root = tmp_path / "downloads"
    metadata = download_root / "pixiv" / "1980643" / "001" / "metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps(_pixiv_metadata(work_id=1)), encoding="utf-8")
    monkeypatch.setattr(settings, "download_root", str(download_root))

    async def no_capacity_wait(_parent_task_id):
        return None

    async def suspended_enqueue(*_args, **_kwargs):
        enqueue_entered.set()
        await asyncio.wait_for(release_enqueue.wait(), timeout=3)
        raise RuntimeError("bounded queue unavailable")

    monkeypatch.setattr(
        disk_import_service,
        "_wait_for_batch_capacity",
        no_capacity_wait,
    )
    redis_client = get_redis()
    redis_client.delete(
        f"task:{task_id}:heartbeat_ts",
        f"task:{task_id}:publisher_fence",
        f"task:{task_id}:publisher:{attempt_a}:heartbeat_ts",
        f"task:{task_id}:publisher:{attempt_a}:fence",
        f"task:{task_id}:publisher:{attempt_b}:heartbeat_ts",
        f"task:{task_id}:publisher:{attempt_b}:fence",
    )
    guard = _DiskImportPublisherGuard(str(task_id))
    guard.attempt_token = attempt_a
    drain_task = None

    try:
        async with async_session() as setup_db:
            await _clear_pipeline_tables(setup_db)
            creator = Creator(name="stale-enqueue-owner")
            setup_db.add(creator)
            await setup_db.flush()
            subscription = Subscription(
                creator_id=creator.id,
                name="Stale enqueue owner",
            )
            setup_db.add(subscription)
            await setup_db.flush()
            repository = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id="1980643",
                source_url="https://www.pixiv.net/users/1980643",
            )
            setup_db.add(repository)
            await setup_db.flush()
            historical = DownloadJob(
                subscription_id=subscription.id,
                subscription_source_id=repository.id,
                source="pixiv",
                source_url=repository.source_url,
                status="complete",
            )
            setup_db.add(historical)
            await setup_db.flush()
            setup_db.add(StorageArtifact(
                storage_root="downloads",
                file_path="pixiv/1980643/001/metadata.json",
                source="pixiv",
                creator_dir="1980643",
                source_work_id="001",
                file_name="metadata.json",
                artifact_type="metadata_json",
                download_job_id=historical.id,
                state="new",
            ))
            await TaskService(setup_db).create_task(
                task_id=task_id,
                kind="admin",
                operation_type="admin-disk-import",
                title="bounded enqueue attempt A",
                status="running",
                queue_name="maintenance",
                meta={
                    "entity": "disk-import",
                    _PUBLISHER_ATTEMPT_META_KEY: attempt_a,
                },
            )
            await setup_db.commit()
            repository_id = repository.id

        stats = {
            "sources": 0,
            "creators": 0,
            "jobs": 0,
            "skipped_done": 0,
            "skipped_invalid_metadata": 0,
            "danbooru_enriched": 0,
            "metadata_fallback": 0,
            "subscription_sources_created": 0,
            "import_job_ids": [],
            "scanned": 0,
            "existing": 0,
            "imported": 0,
            "skipped": 0,
            "failed": 0,
        }

        async def run_drain():
            async with async_session() as publisher_db:
                repository = await publisher_db.get(
                    SubscriptionSource,
                    repository_id,
                )
                try:
                    result = await disk_import_service._drain_pending_ledger(
                        publisher_db,
                        root=download_root,
                        source_filter="pixiv",
                        repository=repository,
                        repository_dirs={"1980643"},
                        parent_task_id=str(task_id),
                        stats=stats,
                        progress_callback=None,
                        enqueue_import=suspended_enqueue,
                        publisher_checkpoint=guard.checkpoint,
                    )
                except PublisherFenceError:
                    return "fenced"
                return ("returned", result)

        drain_task = asyncio.create_task(run_drain())
        await asyncio.wait_for(enqueue_entered.wait(), timeout=3)

        async with async_session() as rotate_db:
            task = (
                await rotate_db.execute(
                    select(TaskRun)
                    .where(TaskRun.id == task_id)
                    .with_for_update(of=TaskRun)
                )
            ).scalar_one()
            task.meta = {
                **dict(task.meta or {}),
                _PUBLISHER_ATTEMPT_META_KEY: attempt_b,
            }
            task.status = "running"
            await rotate_db.commit()
        redis_client.setex(
            f"task:{task_id}:publisher:{attempt_a}:fence",
            300,
            "recovery-owner-a",
        )
        release_enqueue.set()
        outcome = await asyncio.wait_for(drain_task, timeout=5)

        async with async_session() as verify_db:
            recovery_parent = (
                await verify_db.execute(
                    select(DownloadJob).where(
                        DownloadJob.manifest["disk_import_recovery"]
                        .as_boolean()
                        .is_(True)
                    )
                )
            ).scalar_one()
            current_task = await verify_db.get(TaskRun, task_id)
            assert outcome == "fenced"
            assert recovery_parent.status == "downloaded"
            assert recovery_parent.error_log is None
            assert current_task.status == "running"
            assert current_task.meta[_PUBLISHER_ATTEMPT_META_KEY] == attempt_b
    finally:
        release_enqueue.set()
        if drain_task is not None and not drain_task.done():
            drain_task.cancel()
        redis_client.delete(
            f"task:{task_id}:heartbeat_ts",
            f"task:{task_id}:publisher_fence",
            f"task:{task_id}:publisher:{attempt_a}:heartbeat_ts",
            f"task:{task_id}:publisher:{attempt_a}:fence",
            f"task:{task_id}:publisher:{attempt_b}:heartbeat_ts",
            f"task:{task_id}:publisher:{attempt_b}:fence",
        )
        async with async_session() as db:
            await _clear_pipeline_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_downloads_to_db_provisions_metadata_creator_without_placeholder(tmp_path, monkeypatch):
    from app.config import settings
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.download_job import DownloadJob
    from app.models.source_creator import SourceCreator
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.services.disk_import import reconcile_downloads_to_db

    download_root = tmp_path / "downloads"
    work_dir = download_root / "pixiv" / "1980643" / "38362603"
    work_dir.mkdir(parents=True)
    (work_dir / "metadata.json").write_text(json.dumps(_pixiv_metadata()), encoding="utf-8")
    (work_dir / "p0.jpg").write_bytes(b"fixture")
    monkeypatch.setattr(settings, "download_root", str(download_root))
    monkeypatch.setattr("app.services.disk_identity.danbooru_svc.search_and_extract", lambda **_: (None, []))

    async def fake_enqueue(download_job_id: str, import_error: str | None = None, new_json_paths=None):
        return "fallback-import-job"

    monkeypatch.setattr("app.jobs.download._enqueue_import", fake_enqueue)

    try:
        async with async_session() as db:
            await _clear_pipeline_tables(db)

            result = await reconcile_downloads_to_db(
                db,
                {"source": "pixiv", "reset_ledger": True},
            )

            assert result["jobs"] == 1
            assert result["metadata_fallback"] == 1
            job = (await db.execute(select(DownloadJob))).scalar_one()
            assert job.subscription_id is not None
            assert job.subscription_source_id is not None
            assert job.source_url == "https://www.pixiv.net/users/1980643"

            creator = (await db.execute(select(Creator))).scalar_one()
            assert creator.display_name == "Fixture Artist"
            assert creator.name != "recovered"
            # description is never auto-generated; it belongs to the admin
            assert creator.description is None

            sub = (await db.execute(select(Subscription))).scalar_one()
            assert sub.creator_id == creator.id
            assert sub.schedule_mode is None
            assert sub.sync_enabled is True

            source = (await db.execute(select(SubscriptionSource))).scalar_one()
            assert source.subscription_id == sub.id
            assert source.source == "pixiv"
            assert source.is_enabled is True

            sc = (await db.execute(select(SourceCreator))).scalar_one()
            assert sc.creator_id == creator.id
            assert sc.source_creator_id == "1980643"
            assert sc.raw_metadata["_disk_import"]["needs_enrichment"] is True
    finally:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_downloads_to_db_applies_danbooru_enrichment(tmp_path, monkeypatch):
    from app.config import settings
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.creator_link import CreatorLink
    from app.models.source_creator import SourceCreator
    from app.models.subscription_source import SubscriptionSource
    from app.services.disk_import import reconcile_downloads_to_db

    download_root = tmp_path / "downloads"
    work_dir = download_root / "pixiv" / "1980643" / "38362603"
    work_dir.mkdir(parents=True)
    (work_dir / "metadata.json").write_text(json.dumps(_pixiv_metadata()), encoding="utf-8")
    (work_dir / "p0.jpg").write_bytes(b"fixture")
    monkeypatch.setattr(settings, "download_root", str(download_root))
    artist = {
        "id": 12345,
        "name": "fixture_artist",
        "other_names": ["Fixture Artist"],
        "notes": "Known artist",
        "urls": [
            {"url": "https://www.pixiv.net/users/1980643", "normalized_url": "https://www.pixiv.net/users/1980643", "is_active": True},
            {"url": "https://x.com/fixture_artist", "normalized_url": "https://x.com/fixture_artist", "is_active": True},
            {"url": "https://weibo.com/u/123456", "normalized_url": "https://weibo.com/u/123456", "is_active": True},
            {"url": "https://weibo.com/n/fixture_artist", "normalized_url": "https://weibo.com/n/fixture_artist", "is_active": True},
        ],
    }
    links = [{
        "url": "https://www.pixiv.net/users/1980643",
        "link_type": "profile",
        "source": "pixiv",
        "confidence": 1.0,
        "is_verified": True,
        "notes": "Danbooru artist URL",
    }]
    monkeypatch.setattr("app.services.disk_identity.danbooru_svc.search_and_extract", lambda **_: (artist, links))

    async def fake_enqueue(download_job_id: str, import_error: str | None = None, new_json_paths=None):
        return "enriched-import-job"

    monkeypatch.setattr("app.jobs.download._enqueue_import", fake_enqueue)

    try:
        async with async_session() as db:
            await _clear_pipeline_tables(db)

            result = await reconcile_downloads_to_db(
                db,
                {"source": "pixiv", "reset_ledger": True},
            )

            assert result["jobs"] == 1
            assert result["danbooru_enriched"] == 1
            creator = (await db.execute(select(Creator))).scalar_one()
            assert creator.danbooru_artist_id == 12345
            assert (await db.execute(select(func.count(CreatorLink.id)))).scalar_one() == 1
            source_creators = (await db.execute(select(SourceCreator))).scalars().all()
            assert any(sc.source == "pixiv" and sc.source_creator_id == "1980643" for sc in source_creators)
            sources = (await db.execute(select(SubscriptionSource))).scalars().all()
            assert any(ss.source == "pixiv" and ss.is_enabled is True for ss in sources)
            assert any(ss.source == "danbooru" and ss.is_enabled is False for ss in sources)
            assert sum(1 for ss in sources if ss.is_enabled) == 1
            assert sum(1 for ss in sources if ss.source == "weibo") == 1
    finally:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
        await engine.dispose()
