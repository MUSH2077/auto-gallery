import json
import os

import pytest

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

    def target_with_race(relative):
        nonlocal calls
        calls += 1
        resolved = original_target(relative)
        if calls == 2:
            resolved.write_text('{"id": 999}', encoding="utf-8")
        return resolved

    monkeypatch.setattr(stage, "_canonical_target", target_with_race)

    with pytest.raises(DownloadStageConflict):
        stage.promote()

    assert staged.read_text(encoding="utf-8") == '{"id": 1}'
    assert target.read_text(encoding="utf-8") == '{"id": 999}'
    manifest = json.loads(stage.manifest_path.read_text(encoding="utf-8"))
    assert manifest["state"] == "conflict"


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
