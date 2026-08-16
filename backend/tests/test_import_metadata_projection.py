"""Pure contracts for the durable DB-to-metadata projection."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.import_projection import (
    _acquire_metadata_locks,
    _finish_metadata,
    _metadata_lock_path,
    _path_inside,
    _release_metadata_locks,
    process_import_projection_outbox,
)
from app.services.outbox_coordinator import outbox_counts, outbox_health
from app.services.work_import import WorkImportService


def test_library_metadata_projection_is_atomic_and_idempotent(tmp_path: Path):
    source_file = tmp_path / "downloads" / "page.jpg"
    source_file.parent.mkdir()
    source_file.write_bytes(b"image bytes")
    lib_dir = tmp_path / "library" / "pixiv" / "creator" / "100"
    work = SimpleNamespace(
        id=uuid4(),
        title="title",
        posted_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )
    work_source = SimpleNamespace(source="pixiv", source_work_id="100")

    assert WorkImportService.write_library_metadata(
        lib_dir,
        work,
        work_source,
        "Creator",
        [source_file],
    ) is True
    first_stat = (lib_dir / "metadata.json").stat()

    assert WorkImportService.write_library_metadata(
        lib_dir,
        work,
        work_source,
        "Creator",
        [source_file],
    ) is False
    second_stat = (lib_dir / "metadata.json").stat()
    payload = json.loads((lib_dir / "metadata.json").read_text())

    assert second_stat.st_mtime_ns == first_stat.st_mtime_ns
    assert payload["work_id"] == str(work.id)
    assert payload["assets"] == [{"file_name": "page.jpg"}]
    assert not list(lib_dir.glob(".metadata.*.tmp"))


def test_projection_path_rejects_absolute_and_parent_escape(tmp_path: Path):
    assert _path_inside(tmp_path, "pixiv/creator/100/metadata.json").is_relative_to(
        tmp_path.resolve()
    )
    with pytest.raises(ValueError):
        _path_inside(tmp_path, "../outside/metadata.json")
    with pytest.raises(ValueError):
        _path_inside(tmp_path, "/outside/metadata.json")


def test_metadata_target_lock_is_stable_and_nonblocking(monkeypatch, tmp_path: Path):
    import app.services.import_projection as projection_module

    monkeypatch.setattr(
        projection_module.settings,
        "download_root",
        str(tmp_path / "downloads"),
    )
    target = tmp_path / "library" / "pixiv" / "creator" / "100" / "metadata.json"
    row_id = uuid4()
    records = {row_id: {"target": target}}

    assert _metadata_lock_path(target) == _metadata_lock_path(target)
    first = _acquire_metadata_locks(records)
    assert first is not None
    try:
        assert _acquire_metadata_locks(records) is None
    finally:
        _release_metadata_locks(first)


def test_metadata_completion_is_attempt_and_token_fenced():
    source = inspect.getsource(_finish_metadata)

    assert ".with_for_update()" in source
    assert "metadata_lease_token" in source
    assert "metadata_attempts != expected_attempt" in source
    assert "continue" in source


def test_metadata_retry_does_not_reclaim_completed_curation():
    source = inspect.getsource(process_import_projection_outbox)

    assert "payload = await _claim_batch" in source
    assert "metadata_payload = await _claim_metadata_batch" in source
    assert source.index("await _project(payload)") < source.index(
        "metadata_payload = await _claim_metadata_batch"
    )
    assert "await _project(metadata_payload)" not in source


def test_import_outbox_wake_and_health_include_metadata_substate():
    count_source = inspect.getsource(outbox_counts)
    health_source = inspect.getsource(outbox_health)

    assert "metadata_available_at" in count_source
    assert "metadata_lease_expires_at" in count_source
    assert 'result["import_projection"]' in health_source
    assert '"metadata_waiting"' in health_source
    assert '"metadata_processing"' in health_source
    assert '"metadata_failed"' in health_source


def test_metadata_projection_migration_extends_the_single_e8_head():
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "e9c1d3f5a7b0_add_import_metadata_projection.py"
    ).read_text()

    assert 'revision: str = "e9c1d3f5a7b0"' in migration
    assert 'down_revision: Union[str, None] = "e8b0c2d4f6a9"' in migration
    assert "metadata_lease_token UUID" in migration
    assert "metadata_state SET DEFAULT 'pending'" in migration
