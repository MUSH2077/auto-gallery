import json
import os

import pytest

from app.providers.pixiv import PixivProvider
from app.services import download_staging
from app.services.download_staging import DownloadStage, DownloadStageConflict


def metadata_fixture(tmp_path):
    root = tmp_path / "downloads"
    target = root / "pixiv" / "creator" / "work.json"
    target.parent.mkdir(parents=True)
    previous = {"id": 123, "user": {"id": 456}, "page_count": 1}
    replacement = {**previous, "page_count": 2}
    target.write_text(json.dumps(previous))
    stage = DownloadStage.open(root, "durable-boundary", "pixiv")
    staged = stage.root / target.relative_to(root)
    staged.parent.mkdir(parents=True)
    staged.write_text(json.dumps(replacement))
    return root, stage, staged, target, replacement


def test_retained_name_is_durable_before_metadata_source_is_consumed(tmp_path, monkeypatch):
    root, stage, staged, target, replacement = metadata_fixture(tmp_path)
    synchronized = set()
    original_sync = download_staging._fsync_directory
    original_replace = os.replace

    def sync(directory):
        original_sync(directory)
        synchronized.add(directory)

    def replace_then_crash(source, destination):
        if destination == target:
            retained = list((stage.root / download_staging.RETAINED_PROMOTION_DIR).rglob("*"))
            retained_files = [path for path in retained if path.is_file()]
            assert len(retained_files) == 1
            directory = retained_files[0].parent
            while True:
                assert directory in synchronized, "retained name must survive loss of the other directory entries"
                if directory == stage.root:
                    break
                directory = directory.parent
            original_replace(source, destination)
            raise RuntimeError("crash immediately after replacement")
        return original_replace(source, destination)

    monkeypatch.setattr(download_staging, "_fsync_directory", sync)
    monkeypatch.setattr(os, "replace", replace_then_crash)
    with pytest.raises(RuntimeError, match="immediately after replacement"):
        stage.promote(provider=PixivProvider())
    assert not staged.exists()
    # Model loss of the new, not-yet-synced canonical name after that crash.
    target.unlink()
    monkeypatch.undo()
    recovered = DownloadStage.open(root, stage.job_id, "pixiv")
    assert recovered.promote(provider=PixivProvider()).paths == (target,)
    assert json.loads(target.read_text()) == replacement


def test_metadata_target_changed_after_validation_is_not_overwritten(tmp_path, monkeypatch):
    _root, stage, staged, target, replacement = metadata_fixture(tmp_path)
    validate = download_staging._safe_metadata_update
    validations = 0
    unrelated = b'{"id":999,"user":{"id":888}}'

    def swap_after_validation(*args, **kwargs):
        nonlocal validations
        result = validate(*args, **kwargs)
        validations += 1
        if validations == 2:
            newcomer = target.with_suffix(".new")
            newcomer.write_bytes(unrelated)
            os.replace(newcomer, target)
        return result

    monkeypatch.setattr(download_staging, "_safe_metadata_update", swap_after_validation)
    with pytest.raises(DownloadStageConflict):
        stage.promote(provider=PixivProvider())
    assert target.read_bytes() == unrelated
    assert json.loads(staged.read_text()) == replacement


def test_directory_barrier_failure_prevents_source_cleanup(tmp_path, monkeypatch):
    _root, stage, staged, target, _replacement = metadata_fixture(tmp_path)
    original_fsync = os.fsync

    def fail_directory_fsync(fd):
        import stat

        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("durability unavailable")
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    with pytest.raises(OSError, match="durability unavailable"):
        stage.promote(provider=PixivProvider())
    assert staged.exists()
    assert json.loads(target.read_text())["page_count"] == 1
