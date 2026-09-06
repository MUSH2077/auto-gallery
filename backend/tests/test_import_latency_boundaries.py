import io
import json
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.jobs import import_runner
from app.services import job_progress, stage_metrics


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["pause", "cancel"])
async def test_import_control_is_observed_while_hard_pressure_remains(monkeypatch, command):
    listener = SimpleNamespace(command=None, reason="operator request")
    waits = []

    async def limits(_workload):
        return SimpleNamespace(allowed=False), {"status": "paused"}

    async def wake(_workload, seconds, **_kwargs):
        waits.append(seconds)
        assert len(waits) == 1, "control must be checked before another admission wait"
        listener.command = command

    async def state(*_args, **_kwargs):
        pass

    monkeypatch.setattr(import_runner, "current_profile_slice_limits", limits)
    monkeypatch.setattr(import_runner, "set_resource_state", state)
    monkeypatch.setattr("app.services.heavy_io._wait_for_resource_event", wake)
    with pytest.raises(import_runner._ImportControlRequested) as stopped:
        async with import_runner._import_resource_slice("import_db", str(uuid4()), control=listener):
            pytest.fail("critical pressure must never admit the body")
    assert stopped.value.command == command
    assert waits == [2.0]


@pytest.mark.asyncio
async def test_video_import_defers_full_rendering(tmp_path, monkeypatch):
    primary = tmp_path / "video.mp4"
    primary.write_bytes(b"deferred video")
    metadata = tmp_path / "video.mp4.json"
    metadata.write_text("{}")
    destination = tmp_path / "library"
    render = Mock(side_effect=AssertionError("full video rendering ran on import"))
    monkeypatch.setattr(import_runner, "render_video_derivatives", render, raising=False)
    monkeypatch.setattr(import_runner, "media_files_for_group", lambda *_args: [primary])
    monkeypatch.setattr(import_runner.WorkImportService, "library_directory", lambda *_args: ("creator", destination))
    provider = SimpleNamespace(source_name="test", get_creator_directory_name=lambda _raw: "creator")
    prepared = {"items": [], "first_file": metadata, "first_raw": {}, "sc_data": {}}
    result = await import_runner._prepare_work_media(provider, prepared, "work")
    assert result["asset_files"] == [primary]
    assert result["primary_values"] == {}
    render.assert_not_called()


@pytest.mark.asyncio
async def test_failed_first_image_thumbnail_does_not_fail_import(tmp_path, monkeypatch):
    primary = tmp_path / "image.jpg"
    primary.write_bytes(b"an image whose thumbnail fails")
    monkeypatch.setattr(import_runner, "media_files_for_group", lambda *_args: [primary])
    monkeypatch.setattr(import_runner.WorkImportService, "library_directory", lambda *_args: ("creator", tmp_path / "library"))
    monkeypatch.setattr("app.services.thumbnail.inspect_and_generate_thumbnail", Mock(side_effect=RuntimeError("thumbnail failure")))
    provider = SimpleNamespace(source_name="test", get_creator_directory_name=lambda _raw: "creator")
    prepared = {"items": [], "first_file": tmp_path / "image.json", "first_raw": {}, "sc_data": {}}
    result = await import_runner._prepare_work_media(provider, prepared, "work")
    assert result["asset_files"] == [primary]
    assert result["primary_values"] == {}


@pytest.mark.asyncio
async def test_optional_media_without_a_permit_is_not_timed_as_admission(monkeypatch):
    clock = [0.0]
    job = SimpleNamespace(id=uuid4(), execution_token=uuid4())

    async def limits(_workload):
        return SimpleNamespace(allowed=False), {}

    monkeypatch.setattr(import_runner, "current_profile_slice_limits", limits)
    monkeypatch.setattr(stage_metrics.time, "perf_counter", lambda: clock[0])
    with stage_metrics.import_execution_metrics(job.id, job.execution_token):
        async with import_runner._import_resource_slice("image_derive", str(job.id), wait_for_capacity=False) as permitted:
            assert permitted is None
            with stage_metrics.measure_import_phase("media_prepare"):
                clock[0] = 2.0
        progress = job_progress.apply_import_progress(job, "importing", publish=False)
    assert progress["phase_timings_ms"]["admission_wait"] == 0
    assert progress["phase_timings_ms"]["media_prepare"] == 2000


def test_stage_metrics_survive_the_actual_plain_text_log_formatter():
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    previous_level = stage_metrics.logger.level
    stage_metrics.logger.setLevel(logging.INFO)
    stage_metrics.logger.addHandler(handler)
    try:
        with stage_metrics.measure_stage("import_parse", job_id="example"):
            pass
    finally:
        stage_metrics.logger.removeHandler(handler)
        stage_metrics.logger.setLevel(previous_level)
    message = output.getvalue()
    payload = json.loads(message[message.index("{"):])
    assert payload["stage"] == "import_parse"
    assert payload["job_id"] == "example"
    assert payload["wall_seconds"] >= 0


def test_phase_timings_follow_the_owned_execution_only(monkeypatch):
    job_id, token = uuid4(), uuid4()
    job = SimpleNamespace(id=job_id, execution_token=token)
    clock = [0.0]
    monkeypatch.setattr(stage_metrics.time, "perf_counter", lambda: clock[0])
    with stage_metrics.import_execution_metrics(job_id, token, queue_wait_seconds=3):
        with stage_metrics.measure_import_phase("admission_wait"):
            clock[0] = 7.0
        with stage_metrics.measure_import_phase("parse"):
            clock[0] = 7.25
        progress = job_progress.apply_import_progress(job, "importing", publish=False)
        assert progress["phase_timings_ms"] == {"queue_wait": 3000, "admission_wait": 7000, "parse": 250}
        assert progress["execution_token"] == str(token)
        job.execution_token = uuid4()
        stale = job_progress.apply_import_progress(job, "importing", publish=False)
        assert "phase_timings_ms" not in stale
    clean = job_progress.apply_import_progress(SimpleNamespace(id=job_id, execution_token=token), "complete", publish=False)
    assert "phase_timings_ms" not in clean


@pytest.mark.asyncio
async def test_existing_work_updates_obey_each_current_import_budget(monkeypatch):
    active = [False]
    sizes = []

    @asynccontextmanager
    async def capacity(*_args, **_kwargs):
        assert not active[0]
        active[0] = True
        try:
            yield SimpleNamespace(work_units=2)
        finally:
            active[0] = False

    async def update(_provider, _download, prepared):
        assert active[0], "updates must hold the ingest permit"
        sizes.append(len(prepared))
        return {"updated": len(prepared), "unchanged": 0, "assets": 0, "multi_page": 0, "failures": {}}

    monkeypatch.setattr(import_runner, "_import_resource_slice", capacity)
    monkeypatch.setattr(import_runner, "_update_existing_work_groups", update)
    result = await import_runner._update_existing_with_resources(None, None, {str(i): {} for i in range(5)}, "owner")
    assert sizes == [2, 2, 1]
    assert result["updated"] == 5
    assert not active[0]
