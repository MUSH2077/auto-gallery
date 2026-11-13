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


async def _noop_resource_state(*_args, **_kwargs):
    pass


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


@pytest.mark.asyncio
async def test_optional_preview_retries_one_short_busy_window(monkeypatch):
    """A transient busy disk reservation gets one full admission retry."""
    attempts = []
    waits = []
    states = []
    active = [False]

    async def limits(_workload):
        return SimpleNamespace(allowed=True), {"status": "normal"}

    async def state(*args, **kwargs):
        states.append((args, kwargs))

    async def wake(_workload, seconds, **kwargs):
        assert active[0] is False
        waits.append((seconds, kwargs))

    @asynccontextmanager
    async def slot(workload, owner, **kwargs):
        attempts.append((workload, owner, kwargs))
        if len(attempts) == 1:
            raise import_runner.HeavyIOUnavailable("resource_reservation_busy")
        active[0] = True
        try:
            yield SimpleNamespace(work_units=1)
        finally:
            active[0] = False

    monkeypatch.setattr(import_runner, "current_profile_slice_limits", limits)
    monkeypatch.setattr(import_runner, "set_resource_state", state)
    monkeypatch.setattr(import_runner, "heavy_io_slot", slot)
    monkeypatch.setattr("app.services.heavy_io._wait_for_resource_event", wake)

    owner = f"{uuid4()}:{uuid4()}"
    async with import_runner._import_resource_slice(
        "image_derive",
        owner,
        source_identity="manual:work",
        wait_for_capacity=False,
        retry_once_on_busy=True,
    ) as permitted:
        assert permitted is not None
        assert active[0] is True

    assert active[0] is False
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert waits and waits == [(0.05, {"task_id": owner.split(":", 1)[0]})]
    assert any(len(args) > 2 and args[2] == "resource_reservation_busy" for args, _ in states)


@pytest.mark.asyncio
async def test_optional_preview_busy_retry_is_bounded_and_other_rejections_skip(monkeypatch):
    """Busy retries are bounded; pressure/maintenance failures keep old behavior."""
    attempts = []
    waits = []

    async def limits(_workload):
        return SimpleNamespace(allowed=True), {"status": "normal"}

    async def state(*_args, **_kwargs):
        pass

    async def wake(_workload, seconds, **kwargs):
        waits.append((seconds, kwargs))

    @asynccontextmanager
    async def always_busy(workload, owner, **kwargs):
        attempts.append((workload, owner, kwargs))
        raise import_runner.HeavyIOUnavailable("resource_reservation_busy")
        yield  # pragma: no cover - keeps this an async generator

    monkeypatch.setattr(import_runner, "current_profile_slice_limits", limits)
    monkeypatch.setattr(import_runner, "set_resource_state", state)
    monkeypatch.setattr(import_runner, "heavy_io_slot", always_busy)
    monkeypatch.setattr("app.services.heavy_io._wait_for_resource_event", wake)

    async with import_runner._import_resource_slice(
        "image_derive",
        str(uuid4()),
        wait_for_capacity=False,
        retry_once_on_busy=True,
    ) as permitted:
        assert permitted is None

    assert len(attempts) == 2
    assert len(waits) == 1

    attempts.clear()
    waits.clear()

    @asynccontextmanager
    async def maintenance_busy(workload, owner, **kwargs):
        attempts.append((workload, owner, kwargs))
        raise import_runner.HeavyIOUnavailable("maintenance_pending")
        yield  # pragma: no cover - keeps this an async generator

    monkeypatch.setattr(import_runner, "heavy_io_slot", maintenance_busy)
    async with import_runner._import_resource_slice(
        "image_derive",
        str(uuid4()),
        wait_for_capacity=False,
        retry_once_on_busy=True,
    ) as permitted:
        assert permitted is None

    assert len(attempts) == 1
    assert waits == []


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


@pytest.mark.asyncio
async def test_optional_preview_success_does_not_wait(monkeypatch):
    """A normal first admission stays on the existing no-wait path."""
    attempts = []
    waits = []

    async def limits(_workload):
        return SimpleNamespace(allowed=True), {"status": "normal"}

    @asynccontextmanager
    async def slot(workload, owner, **kwargs):
        attempts.append((workload, owner, kwargs))
        yield SimpleNamespace(work_units=1)

    async def wake(*_args, **_kwargs):
        waits.append(True)
        raise AssertionError("a first successful admission must not wait")

    monkeypatch.setattr(import_runner, "current_profile_slice_limits", limits)
    monkeypatch.setattr(import_runner, "set_resource_state", _noop_resource_state)
    monkeypatch.setattr(import_runner, "heavy_io_slot", slot)
    monkeypatch.setattr("app.services.heavy_io._wait_for_resource_event", wake)

    async with import_runner._import_resource_slice(
        "image_derive", str(uuid4()), wait_for_capacity=False, retry_once_on_busy=True
    ) as permitted:
        assert permitted is not None
    assert len(attempts) == 1
    assert waits == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["pause", "cancel"])
async def test_optional_preview_retry_observes_control_before_second_attempt(monkeypatch, command):
    attempts = []
    waits = []
    listener = SimpleNamespace(command=None, reason="operator request")

    async def limits(_workload):
        return SimpleNamespace(allowed=True), {"status": "normal"}

    @asynccontextmanager
    async def slot(workload, owner, **kwargs):
        attempts.append((workload, owner, kwargs))
        raise import_runner.HeavyIOUnavailable("resource_reservation_busy")
        yield  # pragma: no cover - keeps this an async generator

    async def wake(_workload, seconds, **kwargs):
        waits.append((seconds, kwargs))
        listener.command = command

    monkeypatch.setattr(import_runner, "current_profile_slice_limits", limits)
    monkeypatch.setattr(import_runner, "set_resource_state", _noop_resource_state)
    monkeypatch.setattr(import_runner, "heavy_io_slot", slot)
    monkeypatch.setattr("app.services.heavy_io._wait_for_resource_event", wake)

    with pytest.raises(import_runner._ImportControlRequested) as stopped:
        async with import_runner._import_resource_slice(
            "image_derive", str(uuid4()), control=listener,
            wait_for_capacity=False, retry_once_on_busy=True
        ):
            pytest.fail("control must stop before the retry")
    assert stopped.value.command == command
    assert len(attempts) == 1
    assert len(waits) == 1


@pytest.mark.asyncio
async def test_optional_preview_rechecks_pressure_before_retry(monkeypatch):
    attempts = []
    waits = []
    calls = [0]

    async def limits(_workload):
        calls[0] += 1
        if calls[0] == 1:
            return SimpleNamespace(allowed=True), {"status": "normal"}
        return SimpleNamespace(allowed=False), {"status": "paused", "reasons": ["memory"]}

    @asynccontextmanager
    async def slot(workload, owner, **kwargs):
        attempts.append((workload, owner, kwargs))
        raise import_runner.HeavyIOUnavailable("resource_reservation_busy")
        yield  # pragma: no cover - keeps this an async generator

    async def wake(_workload, seconds, **kwargs):
        waits.append((seconds, kwargs))

    monkeypatch.setattr(import_runner, "current_profile_slice_limits", limits)
    monkeypatch.setattr(import_runner, "set_resource_state", _noop_resource_state)
    monkeypatch.setattr(import_runner, "heavy_io_slot", slot)
    monkeypatch.setattr("app.services.heavy_io._wait_for_resource_event", wake)

    async with import_runner._import_resource_slice(
        "image_derive", str(uuid4()), wait_for_capacity=False, retry_once_on_busy=True
    ) as permitted:
        assert permitted is None
    assert calls[0] == 2
    assert len(attempts) == 1
    assert len(waits) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["maintenance_pending", "profile_memory_reserve_live", "redis_lease_unavailable"]
)
async def test_optional_preview_does_not_retry_non_busy_rejections(monkeypatch, reason):
    attempts = []
    waits = []

    async def limits(_workload):
        return SimpleNamespace(allowed=True), {"status": "normal"}

    @asynccontextmanager
    async def slot(workload, owner, **kwargs):
        attempts.append((workload, owner, kwargs))
        raise import_runner.HeavyIOUnavailable(reason)
        yield  # pragma: no cover - keeps this an async generator

    async def wake(_workload, seconds, **kwargs):
        waits.append((seconds, kwargs))

    monkeypatch.setattr(import_runner, "current_profile_slice_limits", limits)
    monkeypatch.setattr(import_runner, "set_resource_state", _noop_resource_state)
    monkeypatch.setattr(import_runner, "heavy_io_slot", slot)
    monkeypatch.setattr("app.services.heavy_io._wait_for_resource_event", wake)

    async with import_runner._import_resource_slice(
        "image_derive", str(uuid4()), wait_for_capacity=False, retry_once_on_busy=True
    ) as permitted:
        assert permitted is None
    assert len(attempts) == 1
    assert waits == []


@pytest.mark.asyncio
async def test_optional_preview_hard_pressure_does_not_wait_for_retry(monkeypatch):
    waits = []

    async def limits(_workload):
        return SimpleNamespace(allowed=False), {"status": "paused", "reasons": ["memory"]}

    async def wake(*args, **kwargs):
        waits.append((args, kwargs))
        raise AssertionError("hard pressure must not enter the short busy retry wait")

    monkeypatch.setattr(import_runner, "current_profile_slice_limits", limits)
    monkeypatch.setattr(import_runner, "set_resource_state", _noop_resource_state)
    monkeypatch.setattr("app.services.heavy_io._wait_for_resource_event", wake)

    async with import_runner._import_resource_slice(
        "image_derive", str(uuid4()), wait_for_capacity=False, retry_once_on_busy=True
    ) as permitted:
        assert permitted is None
    assert waits == []


@pytest.mark.asyncio
async def test_prepare_work_media_uses_provided_asset_files_without_rescanning(tmp_path, monkeypatch):
    primary = tmp_path / "image.jpg"
    primary.write_bytes(b"provided asset")
    metadata = tmp_path / "image.json"
    metadata.write_text("{}")
    monkeypatch.setattr(
        import_runner,
        "media_files_for_group",
        Mock(side_effect=AssertionError("provided asset files must avoid a second scan")),
    )
    monkeypatch.setattr(
        import_runner.WorkImportService,
        "library_directory",
        lambda *_args: ("creator", tmp_path / "library"),
    )
    provider = SimpleNamespace(source_name="test", get_creator_directory_name=lambda _raw: "creator")
    prepared = {"items": [], "first_file": metadata, "first_raw": {}, "sc_data": {}}
    result = await import_runner._prepare_work_media(
        provider, prepared, "work", asset_files=[primary]
    )
    assert result["asset_files"] == [primary]
