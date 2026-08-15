from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select


@asynccontextmanager
async def _test_session():
    """Use only the disposable database selected by tests/conftest.py."""

    from app.database import async_session

    async with async_session() as db:
        yield db


@pytest.mark.integration
@pytest.mark.asyncio
async def test_creator_scope_protects_shared_works_and_soft_delete_stops_sync():
    from app.models import (
        Asset,
        AssetSource,
        Creator,
        CreatorCurationState,
        SourceCreator,
        Subscription,
        SubscriptionSource,
        Work,
        WorkCurationState,
        WorkSource,
    )
    from app.services.hierarchical_deletion import HierarchicalDeletionService

    async with _test_session() as db:
        target = Creator(name="delete-target")
        survivor = Creator(name="shared-survivor")
        db.add_all([target, survivor])
        await db.flush()
        db.add_all([
            SourceCreator(
                creator_id=target.id,
                source="pixiv",
                source_creator_id="target-100",
            ),
            SourceCreator(
                creator_id=survivor.id,
                source="x",
                source_creator_id="survivor-200",
            ),
        ])
        subscription = Subscription(creator_id=target.id, name="Target")
        db.add(subscription)
        await db.flush()
        repository = SubscriptionSource(
            subscription_id=subscription.id,
            source="pixiv",
            source_creator_id="target-100",
            source_url="https://www.pixiv.net/users/target-100",
        )
        exclusive_work = Work(title="exclusive")
        shared_work = Work(title="shared")
        db.add_all([repository, exclusive_work, shared_work])
        await db.flush()
        exclusive_source = WorkSource(
            work_id=exclusive_work.id,
            source="pixiv",
            source_work_id="exclusive-1",
            source_creator_id="target-100",
        )
        shared_target_source = WorkSource(
            work_id=shared_work.id,
            source="pixiv",
            source_work_id="shared-1",
            source_creator_id="target-100",
        )
        shared_survivor_source = WorkSource(
            work_id=shared_work.id,
            source="x",
            source_work_id="shared-2",
            source_creator_id="survivor-200",
        )
        db.add_all([
            exclusive_source,
            shared_target_source,
            shared_survivor_source,
        ])
        await db.flush()
        exclusive_asset = Asset(file_path="exclusive.jpg", file_name="exclusive.jpg")
        shared_asset = Asset(file_path="shared.jpg", file_name="shared.jpg")
        db.add_all([exclusive_asset, shared_asset])
        await db.flush()
        shared_work.thumbnail_asset_id = shared_asset.id
        db.add_all([
            AssetSource(
                asset_id=exclusive_asset.id,
                work_source_id=exclusive_source.id,
                source="pixiv",
                source_asset_id="exclusive-asset",
                ordinal=0,
            ),
            AssetSource(
                asset_id=shared_asset.id,
                work_source_id=exclusive_source.id,
                source="pixiv",
                source_asset_id="shared-asset-pixiv",
                ordinal=1,
            ),
        ])
        await db.flush()

        service = HierarchicalDeletionService(db)
        scope = await service.scope("creator", [target.id])

        assert scope.affected_work_ids == {exclusive_work.id, shared_work.id}
        assert scope.exclusive_work_ids == {exclusive_work.id}
        assert scope.shared_work_ids == {shared_work.id}
        assert scope.exclusive_asset_ids == {exclusive_asset.id}

        result = await service.soft_delete(scope)

        assert result["status"] == "soft_deleted"
        creator_state = (await db.execute(
            select(CreatorCurationState).where(
                CreatorCurationState.creator_id == target.id
            )
        )).scalar_one()
        exclusive_state = (await db.execute(
            select(WorkCurationState).where(
                WorkCurationState.work_id == exclusive_work.id
            )
        )).scalar_one()
        shared_state = (await db.execute(
            select(WorkCurationState).where(
                WorkCurationState.work_id == shared_work.id
            )
        )).scalar_one_or_none()
        assert creator_state.visibility == "archived"
        assert exclusive_state.visibility == "trashed"
        assert shared_state is None
        assert (await db.get(Creator, target.id)).is_active is False
        assert (await db.get(Subscription, subscription.id)).is_active is False
        assert (await db.get(SubscriptionSource, repository.id)).is_enabled is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_paused_download_blocks_repository_deletion_with_structured_conflict():
    from app.models import Creator, DownloadJob, Subscription, SubscriptionSource
    from app.services.hierarchical_deletion import HierarchicalDeletionService

    async with _test_session() as db:
        creator = Creator(name="blocked-target")
        db.add(creator)
        await db.flush()
        subscription = Subscription(creator_id=creator.id)
        db.add(subscription)
        await db.flush()
        repository = SubscriptionSource(
            subscription_id=subscription.id,
            source="pixiv",
            source_creator_id="blocked-100",
            source_url="https://www.pixiv.net/users/blocked-100",
        )
        db.add(repository)
        await db.flush()
        job = DownloadJob(
            subscription_id=subscription.id,
            subscription_source_id=repository.id,
            source="pixiv",
            source_url=repository.source_url,
            status="paused",
        )
        db.add(job)
        await db.flush()

        service = HierarchicalDeletionService(db)
        scope = await service.scope("repository", [repository.id])

        assert scope.active_job_count == 1
        with pytest.raises(HTTPException) as error:
            service.ensure_no_active_jobs(scope)
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "deletion_active_tasks"
        assert str(job.id) in error.value.detail["task_ids"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repository_permanent_delete_removes_domain_jobs_but_keeps_audit_rows():
    from app.models import (
        Creator,
        DownloadJob,
        ImportJob,
        RepositorySyncReceipt,
        StorageArtifact,
        Subscription,
        SubscriptionSource,
        TaskRun,
    )
    from app.services.hierarchical_deletion import HierarchicalDeletionService

    async with _test_session() as db:
        creator = Creator(name="history-target")
        db.add(creator)
        await db.flush()
        subscription = Subscription(creator_id=creator.id)
        db.add(subscription)
        await db.flush()
        repository = SubscriptionSource(
            subscription_id=subscription.id,
            source="pixiv",
            source_creator_id="history-100",
            source_url="https://www.pixiv.net/users/history-100",
        )
        db.add(repository)
        await db.flush()
        download = DownloadJob(
            subscription_id=subscription.id,
            subscription_source_id=repository.id,
            source="pixiv",
            source_url=repository.source_url,
            status="complete",
        )
        db.add(download)
        await db.flush()
        import_job = ImportJob(download_job_id=download.id, status="complete")
        db.add(import_job)
        await db.flush()
        task = TaskRun(
            kind="download",
            subject_type="download_job",
            subject_id=download.id,
            status="complete",
        )
        artifact = StorageArtifact(
            storage_root="library",
            file_path="history/file.jpg",
            source="pixiv",
            creator_dir="history",
            source_work_id="work-1",
            file_name="file.jpg",
            artifact_type="media",
            download_job_id=download.id,
            import_job_id=import_job.id,
        )
        receipt = RepositorySyncReceipt(
            repository_id=repository.id,
            source_download_job_id=download.id,
            source_import_job_id=import_job.id,
            source="pixiv",
            status="complete",
            finished_at=datetime.now(timezone.utc),
        )
        db.add_all([task, artifact, receipt])
        await db.flush()
        task_id = task.id
        artifact_id = artifact.id
        receipt_id = receipt.id
        download_id = download.id
        import_id = import_job.id

        service = HierarchicalDeletionService(db)
        scope = await service.scope("repository", [repository.id])
        assert scope.active_job_count == 0
        result = await service.permanent_delete(scope, delete_files=False)

        assert result["status"] == "complete"
        assert await db.get(SubscriptionSource, repository.id) is None
        assert await db.get(DownloadJob, download_id) is None
        assert await db.get(ImportJob, import_id) is None
        assert await db.get(RepositorySyncReceipt, receipt_id) is None
        preserved_artifact = await db.get(StorageArtifact, artifact_id)
        assert preserved_artifact is not None
        await db.refresh(preserved_artifact)
        assert preserved_artifact.download_job_id is None
        assert preserved_artifact.import_job_id is None
        assert await db.get(TaskRun, task_id) is not None
        assert await db.get(Subscription, subscription.id) is not None


def test_non_admin_cannot_request_hierarchy_file_deletion():
    import asyncio

    from fastapi import Response

    from app.api import creators, repositories, subscriptions

    user = SimpleNamespace(is_admin=False)
    target_id = uuid4()
    calls = (
        creators.delete_creator(
            target_id,
            Response(),
            delete_files=True,
            user=user,
            db=object(),
        ),
        subscriptions.delete_subscription(
            target_id,
            Response(),
            delete_files=True,
            user=user,
            db=object(),
        ),
        repositories.delete_repository(
            target_id,
            Response(),
            delete_files=True,
            user=user,
            db=object(),
        ),
    )
    for call in calls:
        with pytest.raises(HTTPException) as error:
            asyncio.run(call)
        assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_creator_delete_returns_accepted_task(monkeypatch):
    from fastapi import Response

    from app.api import creators
    from app.services import hierarchical_deletion
    from app.services.hierarchical_deletion import DeletionScope

    creator_id = uuid4()
    task_id = uuid4()
    captured = {}

    class FakeDeletionService:
        def __init__(self, db):
            captured["db"] = db

        async def scope(self, entity_type, entity_ids):
            return DeletionScope(
                entity_type=entity_type,
                entity_ids=set(entity_ids),
                creator_ids=set(entity_ids),
            )

        def ensure_no_active_jobs(self, scope):
            captured["scope"] = scope

    async def fake_enqueue(entity_type, entity_ids, *, delete_files):
        captured.update(
            entity_type=entity_type,
            entity_ids=entity_ids,
            delete_files=delete_files,
        )
        return {
            "status": "enqueued",
            "mode": "permanent",
            "entity_type": entity_type,
            "entity_ids": entity_ids,
            "delete_files": delete_files,
            "task_id": task_id,
        }

    monkeypatch.setattr(
        hierarchical_deletion,
        "HierarchicalDeletionService",
        FakeDeletionService,
    )
    monkeypatch.setattr(
        hierarchical_deletion,
        "enqueue_permanent_deletion",
        fake_enqueue,
    )
    monkeypatch.setattr(
        creators,
        "invalidate_creator_subscription_caches",
        lambda **_kwargs: None,
    )
    response = Response()
    result = await creators.delete_creator(
        creator_id,
        response,
        delete_files=True,
        user=SimpleNamespace(is_admin=True),
        db=object(),
    )

    assert response.status_code == 202
    assert result["task_id"] == task_id
    assert captured["entity_type"] == "creator"
    assert captured["entity_ids"] == [creator_id]
    assert captured["delete_files"] is True


@pytest.mark.asyncio
async def test_permanent_delete_rechecks_tasks_after_disabling_target():
    from app.services.hierarchical_deletion import (
        DeletionScope,
        HierarchicalDeletionService,
    )

    entity_id = uuid4()

    class RaceAwareDeletionService(HierarchicalDeletionService):
        def __init__(self):
            self.disabled = False

        async def _disable_targets(self, scope):
            self.disabled = True

        async def scope(self, entity_type, entity_ids):
            assert self.disabled is True
            return DeletionScope(
                entity_type=entity_type,
                entity_ids=set(entity_ids),
                repository_ids={entity_id},
                active_job_count=1,
                active_task_ids={uuid4()},
            )

    initial_scope = DeletionScope(
        entity_type="repository",
        entity_ids={entity_id},
        repository_ids={entity_id},
    )
    with pytest.raises(HTTPException) as error:
        await RaceAwareDeletionService().permanent_delete(
            initial_scope,
            delete_files=False,
        )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "deletion_active_tasks"


def test_hierarchy_work_batches_are_bounded_to_twenty_five():
    from app.services.hierarchical_deletion import HierarchicalDeletionService

    values = {uuid4() for _ in range(51)}
    batches = list(HierarchicalDeletionService._chunks(values))
    assert [len(batch) for batch in batches] == [25, 25, 1]
    assert set().union(*map(set, batches)) == values


def test_deletion_preview_reports_stable_active_task_count():
    from app.services.hierarchical_deletion import DeletionScope

    scope = DeletionScope(
        entity_type="repository",
        entity_ids={uuid4()},
        active_job_count=2,
        active_task_ids={uuid4()},
    )
    assert scope.preview_payload(is_admin=True)["active_task_count"] == 2


@pytest.mark.asyncio
async def test_hierarchy_purge_uses_whole_union_for_cross_batch_asset_ownership(monkeypatch):
    from app.services import hierarchical_deletion
    from app.services.hierarchical_deletion import (
        DeletionScope,
        HierarchicalDeletionService,
    )

    repository_id = uuid4()
    work_ids = {uuid4() for _ in range(26)}
    purge_calls = []

    class FakeResult:
        def all(self):
            return []

    class FakeDB:
        async def execute(self, _statement):
            return FakeResult()

    class FakeCurationService:
        COMMIT_WORK_LIMIT = 25

        def __init__(self, _db):
            pass

        async def trash_works(self, _work_ids, **_kwargs):
            return None

        async def purge(self, purge_ids, **kwargs):
            purge_calls.append((set(purge_ids), set(kwargs["asset_scope_work_ids"])))
            return None

    class FakeDeletionService(HierarchicalDeletionService):
        async def _disable_targets(self, _scope):
            return None

        async def scope(self, entity_type, entity_ids):
            return DeletionScope(
                entity_type=entity_type,
                entity_ids=set(entity_ids),
                repository_ids={repository_id},
                exclusive_work_ids=work_ids,
            )

        async def _delete_domain_rows(self, _scope):
            return None

    monkeypatch.setattr(
        hierarchical_deletion,
        "CurationService",
        FakeCurationService,
    )
    scope = DeletionScope(
        entity_type="repository",
        entity_ids={repository_id},
        repository_ids={repository_id},
        exclusive_work_ids=work_ids,
    )
    await FakeDeletionService(FakeDB()).permanent_delete(
        scope,
        delete_files=True,
    )

    assert [len(call[0]) for call in purge_calls] == [25, 1]
    assert all(call[1] == work_ids for call in purge_calls)


@pytest.mark.asyncio
async def test_inactive_subscription_cannot_admit_manual_repository_sync():
    from app.models import Subscription, SubscriptionSource
    from app.services.subscription_enqueue import enqueue_subscription_source_sync

    source_id = uuid4()
    subscription_id = uuid4()
    source = SimpleNamespace(
        id=source_id,
        subscription_id=subscription_id,
        is_enabled=True,
    )
    subscription = SimpleNamespace(id=subscription_id, is_active=False)

    class FakeDB:
        async def get(self, model, object_id):
            if model is SubscriptionSource and object_id == source_id:
                return source
            if model is Subscription and object_id == subscription_id:
                return subscription
            return None

    result = await enqueue_subscription_source_sync(
        FakeDB(),
        source_id,
        trigger="manual_repository",
    )
    assert result["status"] == "skipped"
    assert result["skip_reason"] == "subscription_inactive"


@pytest.mark.asyncio
async def test_strict_file_cleanup_rejects_paths_outside_managed_roots():
    from app.services.curation import CurationService

    asset = SimpleNamespace(
        file_path="../../outside.jpg",
        thumb_sm_path=None,
        thumb_md_path=None,
        thumb_lg_path=None,
    )
    with pytest.raises(RuntimeError, match="outside managed storage"):
        await CurationService(object())._delete_asset_files(
            asset,
            strict=True,
        )
