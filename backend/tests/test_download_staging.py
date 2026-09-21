import json
import os
import time
from pathlib import Path

import pytest

from app.providers.pixiv import PixivProvider
from app.services import download_staging
from app.services.download_staging import (
    MANIFEST_NAME,
    DownloadStage,
    DownloadStageConflict,
    DownloadStageDiscoveryError,
    DownloadStageManifestError,
)


def test_staging_compatibility_switch_defaults_on(monkeypatch):
    monkeypatch.delenv("DOWNLOAD_STAGING_ENABLED", raising=False)
    assert download_staging.staging_enabled() is True
    monkeypatch.setenv("DOWNLOAD_STAGING_ENABLED", "false")
    assert download_staging.staging_enabled() is False


def test_staging_config_rejects_output_escape():
    with pytest.raises(DownloadStageManifestError, match="base-directory"):
        download_staging.validate_gallerydl_staging_config(
            {"extractor": {"pixiv": {"base-directory": "/outside"}}}
        )
    with pytest.raises(DownloadStageManifestError, match="directory"):
        download_staging.validate_gallerydl_staging_config(
            {"extractor": {"pixiv": {"directory": ["pixiv", "../outside"]}}}
        )


def test_staging_config_keeps_relative_provider_templates():
    download_staging.validate_gallerydl_staging_config(
        {
            "extractor": {
                "pixiv": {
                    "directory": ["pixiv", "{user[id]}", "{id}"],
                    "filename": "{id}_{num}.{extension}",
                }
            }
        }
    )


def test_stage_is_same_volume_and_promotes_only_its_delta(tmp_path):
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    stage = DownloadStage.open(download_root, "job-1", "x")
    staged_metadata = stage.root / "twitter" / "creator" / "one.json"
    staged_media = stage.root / "twitter" / "creator" / "one.jpg"
    staged_metadata.parent.mkdir(parents=True)
    staged_metadata.write_text('{"tweet_id": 1}', encoding="utf-8")
    staged_media.write_bytes(b"image")
    unrelated = download_root / "twitter" / "creator" / "old.jpg"
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_bytes(b"old")

    assert os.stat(stage.root).st_dev == os.stat(download_root).st_dev
    promotion = stage.promote()

    assert {path.relative_to(download_root).as_posix() for path in promotion.paths} == {
        "twitter/creator/one.json",
        "twitter/creator/one.jpg",
    }
    assert unrelated.read_bytes() == b"old"
    stage.mark_registered()
    assert not stage.root.exists()


def test_conflicting_canonical_file_is_never_overwritten(tmp_path):
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    target = download_root / "pixiv" / "creator" / "work.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"canonical")
    stage = DownloadStage.open(download_root, "job-conflict", "pixiv")
    staged = stage.root / target.relative_to(download_root)
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"different")

    with pytest.raises(DownloadStageConflict) as caught:
        stage.promote()

    assert caught.value.conflicts == ["pixiv/creator/work.jpg"]
    assert target.read_bytes() == b"canonical"
    assert staged.read_bytes() == b"different"
    manifest = json.loads(stage.manifest_path.read_text(encoding="utf-8"))
    assert manifest["state"] == "conflict"


@pytest.mark.parametrize(
    ("winner", "expected", "loser"),
    [("staged", b"different", b"canonical"), ("canonical", b"canonical", b"different")],
)
def test_full_conflict_resolution_preserves_loser_and_is_promotable(
    tmp_path, winner, expected, loser,
):
    download_root = tmp_path / "downloads"
    target = download_root / "pixiv" / "creator" / "work.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"canonical")
    stage = DownloadStage.open(download_root, "job-resolve", "pixiv")
    staged = stage.root / target.relative_to(download_root)
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"different")
    with pytest.raises(DownloadStageConflict):
        stage.promote()

    resolution = stage.resolve_conflicts(
        {"pixiv/creator/work.jpg": winner},
        resolution_id=f"resolution-{winner}",
    )

    assert target.read_bytes() == expected
    assert len(resolution.entries) == 1
    quarantine = download_root / resolution.entries[0]["quarantine_path"]
    assert quarantine.read_bytes() == loser
    assert stage.promote().paths == (target,)
    manifest = json.loads(stage.manifest_path.read_text(encoding="utf-8"))
    assert manifest["state"] == "promoted"
    assert manifest["resolution"]["state"] == "applied"


def test_conflict_resolution_resumes_after_atomic_switch_interruption(
    tmp_path, monkeypatch,
):
    download_root = tmp_path / "downloads"
    first_target = download_root / "pixiv" / "creator" / "first.jpg"
    second_target = download_root / "pixiv" / "creator" / "second.jpg"
    first_target.parent.mkdir(parents=True)
    first_target.write_bytes(b"first-canonical")
    second_target.write_bytes(b"second-canonical")
    stage = DownloadStage.open(download_root, "job-resolve-recovery", "pixiv")
    first_staged = stage.root / first_target.relative_to(download_root)
    second_staged = stage.root / second_target.relative_to(download_root)
    first_staged.parent.mkdir(parents=True)
    first_staged.write_bytes(b"first-staged")
    second_staged.write_bytes(b"second-staged")
    with pytest.raises(DownloadStageConflict):
        stage.promote()

    real_replace = os.replace
    interrupted = False

    def replace_then_interrupt(source, destination):
        nonlocal interrupted
        real_replace(source, destination)
        if Path(source) == first_staged and not interrupted:
            interrupted = True
            raise RuntimeError("simulated interruption after conflict switch")

    monkeypatch.setattr(download_staging.os, "replace", replace_then_interrupt)
    decisions = {
        "pixiv/creator/first.jpg": "staged",
        "pixiv/creator/second.jpg": "canonical",
    }
    with pytest.raises(RuntimeError, match="simulated interruption"):
        stage.resolve_conflicts(decisions, resolution_id="interrupted-resolution")

    assert first_target.read_bytes() == b"first-staged"
    assert not first_staged.exists()
    manifest = json.loads(stage.manifest_path.read_text(encoding="utf-8"))
    assert manifest["resolution"]["state"] == "prepared"

    monkeypatch.setattr(download_staging.os, "replace", real_replace)
    recovered = DownloadStage.open(
        download_root,
        "job-resolve-recovery",
        "pixiv",
    )
    resolution = recovered.resolve_conflicts(
        decisions,
        resolution_id="a-new-request-id-is-ignored-while-resuming",
    )

    assert resolution.resolution_id == "interrupted-resolution"
    assert first_target.read_bytes() == b"first-staged"
    assert second_target.read_bytes() == b"second-canonical"
    quarantined = {
        entry["relative_path"]: (
            download_root / entry["quarantine_path"]
        ).read_bytes()
        for entry in resolution.entries
    }
    assert quarantined == {
        "pixiv/creator/first.jpg": b"first-canonical",
        "pixiv/creator/second.jpg": b"second-staged",
    }
    manifest = json.loads(recovered.manifest_path.read_text(encoding="utf-8"))
    assert manifest["resolution"]["state"] == "applied"
    assert {entry["state"] for entry in manifest["resolution"]["entries"]} == {
        "applied"
    }


def test_conflict_resolution_requires_one_decision_per_conflict(tmp_path):
    download_root = tmp_path / "downloads"
    target = download_root / "pixiv" / "creator" / "work.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"canonical")
    stage = DownloadStage.open(download_root, "job-incomplete-resolution", "pixiv")
    staged = stage.root / target.relative_to(download_root)
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"different")
    with pytest.raises(DownloadStageConflict):
        stage.promote()

    with pytest.raises(DownloadStageManifestError, match="exactly one decision"):
        stage.resolve_conflicts({}, resolution_id="missing")

    assert target.read_bytes() == b"canonical"
    assert staged.read_bytes() == b"different"


def test_retry_recovers_crash_after_atomic_link(tmp_path, monkeypatch):
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    stage = DownloadStage.open(download_root, "job-recover", "danbooru")
    staged = stage.root / "danbooru" / "creator" / "work.json"
    staged.parent.mkdir(parents=True)
    staged.write_text('{"id": 1}', encoding="utf-8")
    target = download_root / staged.relative_to(stage.root)
    real_link = os.link
    crashed = False

    def link_then_crash(source, destination, **kwargs):
        nonlocal crashed
        real_link(source, destination, **kwargs)
        if not crashed:
            crashed = True
            raise OSError("simulated crash after link")

    monkeypatch.setattr(download_staging.os, "link", link_then_crash)
    with pytest.raises(download_staging.DownloadStageError):
        stage.promote()
    assert staged.exists() and target.exists()
    assert os.path.samefile(staged, target)

    monkeypatch.setattr(download_staging.os, "link", real_link)
    recovered = DownloadStage.open(download_root, "job-recover", "danbooru")
    promotion = recovered.promote()
    assert promotion.paths == (target,)
    assert not staged.exists()
    assert target.read_text(encoding="utf-8") == '{"id": 1}'
    recovered.mark_registered()
    assert not recovered.root.exists()


def test_incomplete_files_remain_staged_for_gallerydl_retry(tmp_path):
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    stage = DownloadStage.open(download_root, "job-partial", "x")
    partial = stage.root / "twitter" / "creator" / "video.mp4.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"partial")

    assert stage.promote().paths == ()
    stage.mark_registered()

    assert partial.exists()
    assert stage.manifest_path.exists()


def test_completed_retry_file_starts_a_new_batch_after_registered_checkpoint(tmp_path):
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    stage = DownloadStage.open(download_root, "job-partial-completes", "x")
    first = stage.root / "custom" / "first.jpg"
    partial = stage.root / "custom" / "second.jpg.part"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"first")
    partial.write_bytes(b"partial")

    assert stage.promote().paths == (download_root / "custom" / "first.jpg",)
    stage.mark_registered()
    completed = partial.with_suffix("")
    partial.replace(completed)
    completed.write_bytes(b"second-complete")

    recovered = DownloadStage.open(download_root, stage.job_id, "x")
    promotion = recovered.promote()

    assert promotion.paths == (
        download_root / "custom" / "first.jpg",
        download_root / "custom" / "second.jpg",
    )
    assert (download_root / "custom" / "second.jpg").read_bytes() == b"second-complete"
    assert not completed.exists()


def test_corrupt_recovery_manifest_fails_closed(tmp_path):
    download_root = tmp_path / "downloads"
    stage_root = download_root / ".staging" / "job-corrupt"
    stage_root.mkdir(parents=True)
    (stage_root / MANIFEST_NAME).write_text("not-json", encoding="utf-8")
    (stage_root / "only-copy.json").write_text("{}", encoding="utf-8")

    with pytest.raises(DownloadStageManifestError):
        DownloadStage.open(download_root, "job-corrupt", "x")

    assert (stage_root / "only-copy.json").exists()


def test_target_appearing_after_preflight_is_rechecked(tmp_path, monkeypatch):
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    stage = DownloadStage.open(download_root, "job-runtime-race", "pixiv")
    staged = stage.root / "pixiv" / "creator" / "work.json"
    staged.parent.mkdir(parents=True)
    staged.write_text('{"id": 1}', encoding="utf-8")
    target = download_root / staged.relative_to(stage.root)
    original_target = stage._canonical_target
    calls = 0

    def target_with_race(relative, **kwargs):
        nonlocal calls
        calls += 1
        resolved = original_target(relative, **kwargs)
        if calls == 2:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text('{"id": 999}', encoding="utf-8")
        return resolved

    monkeypatch.setattr(stage, "_canonical_target", target_with_race)

    with pytest.raises(DownloadStageConflict):
        stage.promote()

    assert staged.read_text(encoding="utf-8") == '{"id": 1}'
    assert target.read_text(encoding="utf-8") == '{"id": 999}'
    manifest = json.loads(stage.manifest_path.read_text(encoding="utf-8"))
    assert manifest["state"] == "conflict"


def test_identical_target_replaced_during_boundary_check_keeps_staged_copy(
    tmp_path, monkeypatch,
):
    download_root = tmp_path / "downloads"
    target = download_root / "custom" / "work.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"same")
    stage = DownloadStage.open(download_root, "job-identical-race", "x")
    staged = stage.root / target.relative_to(download_root)
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"same")
    real_files_equal = download_staging._files_equal
    comparisons = 0

    def compare_then_replace(first, second):
        nonlocal comparisons
        comparisons += 1
        equal = real_files_equal(first, second)
        if comparisons == 2:
            replacement = target.with_suffix(".incoming")
            replacement.write_bytes(b"unrelated")
            os.replace(replacement, target)
        return equal

    monkeypatch.setattr(download_staging, "_files_equal", compare_then_replace)

    with pytest.raises(DownloadStageConflict):
        stage.promote()

    assert staged.read_bytes() == b"same"
    assert target.read_bytes() == b"unrelated"


def test_metadata_discovery_failure_keeps_recovery_manifest(tmp_path):
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    stage = DownloadStage.open(download_root, "job-bad-metadata", "pixiv")
    invalid = download_root / "pixiv" / "creator" / "invalid.json"
    invalid.parent.mkdir(parents=True)
    invalid.write_text("not provider metadata", encoding="utf-8")

    stage.mark_discovery_failed([invalid])
    error = DownloadStageDiscoveryError(["pixiv/creator/invalid.json"])

    assert "invalid.json" in str(error)
    assert stage.manifest_path.exists()
    manifest = json.loads(stage.manifest_path.read_text(encoding="utf-8"))
    assert manifest["state"] == "discovery_failed"
    assert manifest["invalid_metadata"] == ["pixiv/creator/invalid.json"]


def _pixiv_metadata(*, creator_id="12539859", page_count=1, bookmarks=10):
    return {
        "id": 148166622,
        "title": "愛彌斯",
        "page_count": page_count,
        "count": page_count,
        "bookmarks": bookmarks,
        "user": {"id": int(creator_id), "account": "ruzhaiii", "name": "ruzhaiii"},
    }


def test_same_work_metadata_update_is_auditable_and_adds_new_page(tmp_path):
    download_root = tmp_path / "downloads"
    target_dir = download_root / "pixiv" / "ruzhaiii" / "148166622"
    target_dir.mkdir(parents=True)
    target_json = target_dir / "148166622_p0.json"
    target_json.write_text(json.dumps(_pixiv_metadata()), encoding="utf-8")
    target_media = target_dir / "148166622_p0.jpg"
    target_media.write_bytes(b"same-image")

    stage = DownloadStage.open(download_root, "job-upstream-edit", "pixiv")
    staged_dir = stage.root / target_dir.relative_to(download_root)
    staged_dir.mkdir(parents=True)
    (staged_dir / target_json.name).write_text(
        json.dumps(_pixiv_metadata(page_count=2, bookmarks=99)),
        encoding="utf-8",
    )
    (staged_dir / target_media.name).write_bytes(b"same-image")
    (staged_dir / "148166622_p1.json").write_text(
        json.dumps(_pixiv_metadata(page_count=2, bookmarks=99)),
        encoding="utf-8",
    )
    (staged_dir / "148166622_p1.jpg").write_bytes(b"new-page")

    promotion = stage.promote(provider=PixivProvider())

    assert json.loads(target_json.read_text(encoding="utf-8"))["page_count"] == 2
    assert (target_dir / "148166622_p1.jpg").read_bytes() == b"new-page"
    assert len(promotion.metadata_updates) == 1
    update = promotion.metadata_updates[0]
    assert update["classification"] == "same_work_metadata_update"
    assert update["source_work_id"] == "148166622"
    assert update["source_creator_id"] == "12539859"
    assert update["previous_metadata"]["page_count"] == 1
    assert "page_count" in update["changed_fields"]


def test_metadata_update_with_different_creator_remains_conflict(tmp_path):
    download_root = tmp_path / "downloads"
    target = download_root / "pixiv" / "ruzhaiii" / "148166622" / "148166622_p0.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(_pixiv_metadata()), encoding="utf-8")
    stage = DownloadStage.open(download_root, "job-identity-conflict", "pixiv")
    staged = stage.root / target.relative_to(download_root)
    staged.parent.mkdir(parents=True)
    staged.write_text(
        json.dumps(_pixiv_metadata(creator_id="999", page_count=2)),
        encoding="utf-8",
    )

    with pytest.raises(DownloadStageConflict) as caught:
        stage.promote(provider=PixivProvider())

    assert caught.value.details[0]["file_type"] == "metadata"
    assert json.loads(target.read_text(encoding="utf-8"))["user"]["id"] == 12539859


def test_safe_metadata_replace_recovers_after_manifest_update_gap(tmp_path, monkeypatch):
    download_root = tmp_path / "downloads"
    target = download_root / "pixiv" / "ruzhaiii" / "148166622" / "148166622_p0.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(_pixiv_metadata()), encoding="utf-8")
    stage = DownloadStage.open(download_root, "job-metadata-recover", "pixiv")
    staged = stage.root / target.relative_to(download_root)
    staged.parent.mkdir(parents=True)
    staged.write_text(json.dumps(_pixiv_metadata(page_count=2)), encoding="utf-8")

    original_sync = download_staging._fsync_directory

    def crash_after_replace(path):
        if not staged.exists():
            raise RuntimeError("simulated crash after metadata replace")
        original_sync(path)

    monkeypatch.setattr(download_staging, "_fsync_directory", crash_after_replace)
    with pytest.raises(RuntimeError, match="simulated crash"):
        stage.promote(provider=PixivProvider())
    assert not staged.exists()
    assert json.loads(target.read_text(encoding="utf-8"))["page_count"] == 2

    monkeypatch.undo()
    recovered = DownloadStage.open(download_root, "job-metadata-recover", "pixiv")
    promotion = recovered.promote(provider=PixivProvider())
    assert promotion.paths == (target,)
    assert promotion.metadata_updates[0]["state"] == "applied"


@pytest.mark.parametrize("checkpoint_side", ["before", "after"])
def test_recovery_plan_checkpoint_crash_precedes_canonical_mutations(
    tmp_path, monkeypatch, checkpoint_side,
):
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    stage = DownloadStage.open(download_root, f"job-plan-{checkpoint_side}", "x")
    staged = stage.root / "custom" / "asset.jpg"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"planned")
    target = download_root / staged.relative_to(stage.root)
    original_write = stage._write_manifest

    def crash_at_plan_checkpoint():
        if checkpoint_side == "before":
            raise RuntimeError("before recovery plan checkpoint")
        original_write()
        raise RuntimeError("after recovery plan checkpoint")

    monkeypatch.setattr(stage, "_write_manifest", crash_at_plan_checkpoint)
    with pytest.raises(RuntimeError, match="recovery plan checkpoint"):
        stage.promote()

    assert staged.read_bytes() == b"planned"
    assert not target.exists()

    monkeypatch.undo()
    recovered = DownloadStage.open(download_root, stage.job_id, "x")
    assert recovered.promote().paths == (target,)
    assert target.read_bytes() == b"planned"


@pytest.mark.parametrize("checkpoint_side", ["before", "after"])
def test_batch_checkpoint_crash_recovers_without_losing_staged_files(
    tmp_path, monkeypatch, checkpoint_side,
):
    """A missing final batch checkpoint must leave every source replayable."""

    download_root = tmp_path / "downloads"
    download_root.mkdir()
    stage = DownloadStage.open(download_root, f"job-checkpoint-{checkpoint_side}", "x")
    staged_paths = []
    for index in range(3):
        staged = stage.root / "custom" / "layout" / f"{index}.jpg"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(f"payload-{index}".encode())
        staged_paths.append(staged)

    original_write = stage._write_manifest
    writes = 0

    def crash_at_final_checkpoint():
        nonlocal writes
        writes += 1
        if writes == 2 and checkpoint_side == "before":
            raise RuntimeError("before promoted checkpoint")
        original_write()
        if writes == 2 and checkpoint_side == "after":
            raise RuntimeError("after promoted checkpoint")

    monkeypatch.setattr(stage, "_write_manifest", crash_at_final_checkpoint)
    with pytest.raises(RuntimeError, match="promoted checkpoint"):
        stage.promote()

    targets = [download_root / path.relative_to(stage.root) for path in staged_paths]
    assert all(path.exists() for path in targets)
    assert all(path.exists() for path in staged_paths)

    monkeypatch.undo()
    recovered = DownloadStage.open(download_root, stage.job_id, "x")
    promotion = recovered.promote()
    assert promotion.paths == tuple(targets)
    assert [path.read_bytes() for path in targets] == [
        b"payload-0",
        b"payload-1",
        b"payload-2",
    ]
    assert all(not path.exists() for path in staged_paths)


def test_retry_repeats_directory_barriers_before_promoted_checkpoint(
    tmp_path, monkeypatch,
):
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    stage = DownloadStage.open(download_root, "job-directory-barrier", "x")
    staged = stage.root / "custom" / "nested" / "asset.jpg"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"asset")

    def interrupt_first_directory_barrier(_path):
        raise RuntimeError("directory barrier interrupted")

    monkeypatch.setattr(download_staging, "_fsync_directory", interrupt_first_directory_barrier)
    with pytest.raises(RuntimeError, match="directory barrier interrupted"):
        stage.promote()

    monkeypatch.undo()
    recovered = DownloadStage.open(download_root, stage.job_id, "x")
    barriers = []
    final_checkpoint_saw_barrier = False
    original_write = recovered._write_manifest

    def record_barrier(path):
        barriers.append(path)

    def record_final_checkpoint():
        nonlocal final_checkpoint_saw_barrier
        final_checkpoint_saw_barrier = bool(barriers)
        original_write()

    monkeypatch.setattr(download_staging, "_fsync_directory", record_barrier)
    monkeypatch.setattr(recovered, "_write_manifest", record_final_checkpoint)
    assert recovered.promote().paths == (download_root / "custom" / "nested" / "asset.jpg",)

    assert final_checkpoint_saw_barrier
    assert download_root / "custom" / "nested" in barriers


def test_existing_identical_target_is_retained_until_batch_checkpoint(
    tmp_path, monkeypatch,
):
    download_root = tmp_path / "downloads"
    target = download_root / "custom" / "same.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"same")
    stage = DownloadStage.open(download_root, "job-identical-checkpoint", "x")
    staged = stage.root / target.relative_to(download_root)
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"same")
    original_write = stage._write_manifest
    writes = 0

    def crash_before_promoted_checkpoint():
        nonlocal writes
        writes += 1
        if writes == 2:
            raise RuntimeError("before promoted checkpoint")
        original_write()

    monkeypatch.setattr(stage, "_write_manifest", crash_before_promoted_checkpoint)
    with pytest.raises(RuntimeError, match="before promoted checkpoint"):
        stage.promote()

    assert staged.read_bytes() == b"same"
    assert target.read_bytes() == b"same"

    monkeypatch.undo()
    recovered = DownloadStage.open(download_root, stage.job_id, "x")
    assert recovered.promote().paths == (target,)
    assert not staged.exists()


def test_metadata_replacement_retains_recoverable_stage_copy_until_checkpoint(
    tmp_path, monkeypatch,
):
    download_root = tmp_path / "downloads"
    target = download_root / "pixiv" / "custom" / "148166622_p0.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(_pixiv_metadata()), encoding="utf-8")
    stage = DownloadStage.open(download_root, "job-retained-metadata", "pixiv")
    staged = stage.root / target.relative_to(download_root)
    staged.parent.mkdir(parents=True)
    replacement = json.dumps(_pixiv_metadata(page_count=2)).encode()
    staged.write_bytes(replacement)

    original_write = stage._write_manifest
    writes = 0

    def crash_before_promoted_checkpoint():
        nonlocal writes
        writes += 1
        if writes == 2:
            raise RuntimeError("before promoted checkpoint")
        original_write()

    monkeypatch.setattr(stage, "_write_manifest", crash_before_promoted_checkpoint)
    with pytest.raises(RuntimeError, match="before promoted checkpoint"):
        stage.promote(provider=PixivProvider())

    retained = [
        path
        for path in stage.root.rglob("*")
        if path.is_file() and path != stage.manifest_path
    ]
    assert retained
    assert any(path.read_bytes() == replacement for path in retained)
    assert target.read_bytes() == replacement

    # Model loss of an uncheckpointed canonical directory entry. Recovery must
    # still be possible from the retained stage name.
    target.unlink()
    monkeypatch.undo()
    recovered = DownloadStage.open(download_root, stage.job_id, "pixiv")
    promotion = recovered.promote(provider=PixivProvider())
    assert promotion.paths == (target,)
    assert target.read_bytes() == replacement
    assert promotion.metadata_updates[0]["state"] == "applied"


def test_cleanup_interruption_replays_from_durable_promoted_checkpoint(
    tmp_path, monkeypatch,
):
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    stage = DownloadStage.open(download_root, "job-cleanup-crash", "x")
    staged_paths = []
    for index in range(3):
        staged = stage.root / "arbitrary" / f"{index}.jpg"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(f"asset-{index}".encode())
        staged_paths.append(staged)

    real_unlink = Path.unlink
    interrupted = False

    def unlink_then_interrupt(path, *args, **kwargs):
        nonlocal interrupted
        result = real_unlink(path, *args, **kwargs)
        if path in staged_paths and not interrupted:
            interrupted = True
            raise RuntimeError("cleanup interrupted")
        return result

    monkeypatch.setattr(Path, "unlink", unlink_then_interrupt)
    with pytest.raises(RuntimeError, match="cleanup interrupted"):
        stage.promote()

    durable = json.loads(stage.manifest_path.read_text(encoding="utf-8"))
    assert durable["state"] == "promoted"
    targets = [download_root / path.relative_to(stage.root) for path in staged_paths]
    assert all(path.exists() for path in targets)

    monkeypatch.undo()
    recovered = DownloadStage.open(download_root, stage.job_id, "x")
    assert recovered.promote().paths == tuple(targets)
    assert all(not path.exists() for path in staged_paths)


def test_old_v1_partially_promoted_manifest_is_recoverable(tmp_path, monkeypatch):
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    stage = DownloadStage.open(download_root, "job-old-v1", "x")
    staged = stage.root / "legacy-layout" / "work.jpg"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"legacy")
    target = download_root / staged.relative_to(stage.root)
    target.parent.mkdir(parents=True)
    os.link(staged, target)

    identity = download_staging._file_identity(staged)
    old_manifest = {
        "version": 1,
        "job_id": stage.job_id,
        "source": "x",
        "state": "promoting",
        "planned": {"legacy-layout/work.jpg": identity},
        "promoted": {},
        "conflicts": [],
        "created_at": "2026-09-06T00:00:00+00:00",
        "updated_at": "2026-09-06T00:00:00+00:00",
    }
    stage.manifest_path.write_text(json.dumps(old_manifest), encoding="utf-8")

    synchronized = set()
    original_sync = download_staging._fsync_directory

    def sync(directory):
        original_sync(directory)
        synchronized.add(directory)

    monkeypatch.setattr(download_staging, "_fsync_directory", sync)

    recovered = DownloadStage.open(download_root, stage.job_id, "x")
    assert recovered.promote().paths == (target,)
    assert target.read_bytes() == b"legacy"
    assert not staged.exists()
    assert target.parent in synchronized


@pytest.mark.parametrize("file_count", [10, 100, 1000])
def test_batch_promotion_metadata_io_grows_linearly(
    tmp_path, monkeypatch, file_count,
):
    """Actual file promotion uses a constant number of full-manifest commits."""

    download_root = tmp_path / "downloads"
    download_root.mkdir()
    stage = DownloadStage.open(download_root, f"job-linear-{file_count}", "x")
    staged_dir = stage.root / "custom-template" / "one-directory"
    staged_dir.mkdir(parents=True)
    expected = {}
    for index in range(file_count):
        relative = f"custom-template/one-directory/{index:04d}.jpg"
        payload = f"payload-{index}".encode()
        (stage.root / relative).write_bytes(payload)
        expected[relative] = payload

    manifest_writes = []
    fsync_count = 0
    original_write = stage._write_manifest
    real_fsync = os.fsync

    def measured_write():
        original_write()
        manifest_writes.append(stage.manifest_path.stat().st_size)

    def measured_fsync(fd):
        nonlocal fsync_count
        fsync_count += 1
        return real_fsync(fd)

    monkeypatch.setattr(stage, "_write_manifest", measured_write)
    monkeypatch.setattr(download_staging.os, "fsync", measured_fsync)
    started = time.perf_counter()
    promotion = stage.promote()
    elapsed = time.perf_counter() - started

    assert len(promotion.paths) == file_count
    assert len(manifest_writes) <= 2
    assert sum(manifest_writes) <= 12_000 * file_count + 20_000
    # Two manifest commits use two fsyncs each; the three canonical directory
    # levels are each synchronized once regardless of file count.
    assert fsync_count <= 8
    assert {
        path.relative_to(download_root).as_posix(): path.read_bytes()
        for path in promotion.paths
    } == expected
    print(
        f"staging files={file_count} elapsed={elapsed:.6f}s "
        f"manifest_writes={len(manifest_writes)} "
        f"manifest_bytes={sum(manifest_writes)} fsyncs={fsync_count}"
    )
