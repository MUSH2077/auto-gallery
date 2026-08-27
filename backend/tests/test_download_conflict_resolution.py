import inspect
import json
import os

import pytest


def _identity(**changes):
    value = {
        "source": "pixiv",
        "source_work_id": "146520001",
        "source_creator_id": "40589627",
        "source_asset_id": "146520001_p0",
        "ordinal": 0,
    }
    value.update(changes)
    return value


def test_auto_resolution_requires_source_work_creator_page_and_repository_evidence():
    from app.services.download_conflicts import upstream_conflict_evidence

    evidence = upstream_conflict_evidence(
        job_source="pixiv",
        repository_identity={"source": "pixiv", "source_creator_id": "40589627"},
        database_identity=_identity(),
        canonical_metadata_identity=_identity(),
        staged_metadata_identity=_identity(),
        filename_ordinal=0,
    )

    assert evidence["auto_eligible"] is True
    assert evidence["recommended_winner"] == "staged"
    assert evidence["checks"] == {
        "source": True,
        "repository": True,
        "work": True,
        "creator": True,
        "page": True,
        "source_asset": True,
    }


def test_auto_resolution_fails_closed_on_cross_source_work_or_page_identity():
    from app.services.download_conflicts import upstream_conflict_evidence

    for staged in (
        _identity(source="x"),
        _identity(source_work_id="different"),
        _identity(source_creator_id="different"),
        _identity(source_asset_id="different"),
        _identity(ordinal=1),
    ):
        evidence = upstream_conflict_evidence(
            job_source="pixiv",
            repository_identity={"source": "pixiv", "source_creator_id": "40589627"},
            database_identity=_identity(),
            canonical_metadata_identity=_identity(),
            staged_metadata_identity=staged,
            filename_ordinal=0,
        )
        assert evidence["auto_eligible"] is False
        assert evidence["recommended_winner"] == "canonical"


def test_resolved_staging_conflict_can_retry_but_unresolved_one_cannot():
    from app.services.task_engine import TaskEngine

    source = inspect.getsource(TaskEngine.retry_download)
    assert "staging_conflict_resolved" in source
    assert "latest_resolution_index" in source


def test_conflict_resolution_routes_are_admin_mutations_and_task_read_views():
    from app.api.tasks import router

    paths = {route.path: route for route in router.routes}
    assert "/{task_id}/conflicts" in paths
    assert "/{task_id}/conflicts/media" in paths
    assert "/{task_id}/conflicts/resolve" in paths
    assert "/{task_id}/conflicts/resolutions/{resolution_id}/rollback" in paths


def test_rollback_resumes_after_atomic_switch_interruption(tmp_path, monkeypatch):
    from app.services import download_conflicts
    from app.services.download_conflicts import DownloadConflictService
    from app.services.download_staging import DownloadStage, DownloadStageConflict

    download_root = tmp_path / "downloads"
    monkeypatch.setattr(download_conflicts.settings, "download_root", str(download_root))
    relative = "pixiv/artist/work.jpg"
    target = download_root / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(b"canonical")
    stage = DownloadStage.open(download_root, "rollback-recovery", "pixiv")
    staged = stage.root / relative
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"staged")
    with pytest.raises(DownloadStageConflict):
        stage.promote()
    resolution = stage.resolve_conflicts(
        {relative: "staged"},
        resolution_id="rollback-resolution",
    )
    entry = dict(resolution.entries[0])
    quarantine = download_root / entry["quarantine_path"]

    real_replace = os.replace
    interrupted = False

    def replace_then_interrupt(source, destination):
        nonlocal interrupted
        real_replace(source, destination)
        if source == quarantine and not interrupted:
            interrupted = True
            raise RuntimeError("simulated rollback interruption")

    monkeypatch.setattr(download_conflicts.os, "replace", replace_then_interrupt)
    service = DownloadConflictService(None)
    with pytest.raises(RuntimeError, match="simulated rollback interruption"):
        service._rollback_files(entry, resolution_id="rollback-resolution")

    assert target.read_bytes() == b"canonical"
    assert not quarantine.exists()

    monkeypatch.setattr(download_conflicts.os, "replace", real_replace)
    service._rollback_files(entry, resolution_id="rollback-resolution")

    assert target.read_bytes() == b"canonical"
    assert quarantine.read_bytes() == b"staged"


def test_managed_conflict_reader_rejects_symlinked_parent(tmp_path):
    from app.services.download_conflicts import (
        DownloadConflictError,
        _open_managed_regular,
    )

    root = tmp_path / "downloads"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "work.jpg").write_bytes(b"outside")
    (root / "pixiv").symlink_to(outside, target_is_directory=True)

    with pytest.raises(DownloadConflictError, match="unsafe"):
        _open_managed_regular(root, "pixiv/work.jpg")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_evidence_backed_resolution_updates_asset_and_rollback_restores_it(
    tmp_path, monkeypatch,
):
    from app.database import async_session
    from app.models import (
        Asset,
        AssetSource,
        Creator,
        DownloadJob,
        Subscription,
        SubscriptionSource,
        TaskRun,
        Work,
        WorkSource,
    )
    from app.services import download_conflicts
    from app.services.download_conflicts import DownloadConflictService
    from app.services.download_staging import DownloadStage, DownloadStageConflict

    download_root = tmp_path / "downloads"
    monkeypatch.setattr(download_conflicts.settings, "download_root", str(download_root))
    relative = "pixiv/artist/146520001/146520001_p0.jpg"
    canonical = download_root / relative
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"old-upstream-bytes")
    metadata = {
        "id": 146520001,
        "title": "updated work",
        "num": 0,
        "user": {"id": 40589627, "account": "artist", "name": "Artist"},
    }
    (canonical.parent / "146520001_p0.json").write_text(json.dumps(metadata), encoding="utf-8")

    async with async_session() as db:
        creator = Creator(name="conflict-test")
        db.add(creator)
        await db.flush()
        subscription = Subscription(creator_id=creator.id)
        work = Work(title="old work")
        db.add_all([subscription, work])
        await db.flush()
        repository = SubscriptionSource(
            subscription_id=subscription.id,
            source="pixiv",
            source_creator_id="40589627",
            source_url="https://www.pixiv.net/users/40589627",
        )
        work_source = WorkSource(
            work_id=work.id,
            source="pixiv",
            source_work_id="146520001",
            source_creator_id="40589627",
        )
        asset = Asset(
            file_path=relative,
            file_name=canonical.name,
            file_size=canonical.stat().st_size,
            sha256="old-db-hash",
        )
        db.add_all([repository, work_source, asset])
        await db.flush()
        db.add(AssetSource(
            asset_id=asset.id,
            work_source_id=work_source.id,
            source="pixiv",
            source_asset_id=canonical.stem,
            ordinal=0,
            role="page",
        ))
        job = DownloadJob(
            subscription_id=subscription.id,
            subscription_source_id=repository.id,
            source="pixiv",
            source_url=repository.source_url,
            status="failed",
        )
        db.add(job)
        await db.flush()
        task = TaskRun(
            kind="download",
            operation_type="download",
            subject_type="download_job",
            subject_id=job.id,
            status="failed",
            reason_code="download_staging_conflict",
            attention_state="open",
        )
        db.add(task)
        await db.flush()

        stage = DownloadStage.open(download_root, str(job.id), "pixiv")
        staged = stage.root / relative
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"new-upstream-bytes")
        (staged.parent / "146520001_p0.json").write_text(json.dumps(metadata), encoding="utf-8")
        with pytest.raises(DownloadStageConflict):
            stage.promote()

        service = DownloadConflictService(db)
        case = await service.inspect(task.id)
        assert case["all_auto_eligible"] is True
        result = await service.resolve(
            task.id,
            {relative: "staged"},
            operator="test",
            automatic=True,
            resolution_id="integration-resolution",
        )

        assert canonical.read_bytes() == b"new-upstream-bytes"
        assert asset.sha256 == result["entries"][0]["winner_sha256"]
        await service.rollback(task.id, "integration-resolution", operator="test")
        assert canonical.read_bytes() == b"old-upstream-bytes"
        assert asset.sha256 == "old-db-hash"
        await db.rollback()
