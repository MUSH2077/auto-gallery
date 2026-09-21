from __future__ import annotations

import json
import os
from io import StringIO
import threading
from uuid import uuid4

import pytest


class _IdleThread:
    def __init__(self, *args, **kwargs):
        self.started = False

    def start(self):
        self.started = True

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


def test_heartbeat_start_publishes_before_background_thread(monkeypatch):
    """A newly supervised task gets a heartbeat without waiting one interval."""

    from app.jobs import worker_control

    publications: list[int] = []
    monkeypatch.setattr(worker_control.threading, "Thread", _IdleThread)
    monkeypatch.setattr(
        worker_control.TaskEventPublisher,
        "publish_heartbeat",
        lambda _job_id, _task_type, *, pid: publications.append(pid) or True,
    )

    heartbeat = worker_control.HeartbeatPublisher(
        "download-job",
        "download",
        pid=4242,
    )
    heartbeat.start()

    assert publications == [4242]


def test_immediate_heartbeat_failure_keeps_ordinary_publisher_retrying(monkeypatch):
    """A transient Redis error at startup must not abort the supervised job."""

    from app.jobs import worker_control

    threads: list[_IdleThread] = []

    def make_thread(*args, **kwargs):
        thread = _IdleThread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(worker_control.threading, "Thread", make_thread)
    monkeypatch.setattr(
        worker_control.TaskEventPublisher,
        "publish_heartbeat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("redis")),
    )

    heartbeat = worker_control.HeartbeatPublisher(
        "download-job",
        "download",
        pid=4242,
    )
    heartbeat.start()

    assert len(threads) == 1
    assert threads[0].started is True


def test_signal_process_group_never_guesses_after_child_pid_is_reaped(monkeypatch):
    """A missing child pid cannot be reused as an inferred process-group id."""

    from app.jobs import worker_control

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        worker_control.os,
        "getpgid",
        lambda _pid: (_ for _ in ()).throw(ProcessLookupError()),
    )
    monkeypatch.setattr(
        worker_control.os,
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)),
    )

    assert worker_control.signal_process_group(4242, 15) is False
    assert signals == []


def test_heartbeat_publishes_immediately_and_transfers_to_worker_pid(monkeypatch):
    """Starting or transferring supervision must refresh liveness immediately."""

    from app.jobs import worker_control

    now = {"value": 0.0}
    publications: list[tuple[float, int]] = []

    monkeypatch.setattr(worker_control.threading, "Thread", _IdleThread)
    monkeypatch.setattr(
        worker_control.TaskEventPublisher,
        "publish_heartbeat",
        lambda _job_id, _task_type, *, pid: publications.append(
            (now["value"], pid)
        )
        or True,
    )

    heartbeat = worker_control.HeartbeatPublisher(
        "download-job",
        "download",
        pid=4242,
    )
    heartbeat.start()
    now["value"] = 5.0
    heartbeat.transfer_to_pid(9001)

    assert publications == [(0.0, 4242), (5.0, 9001)]


def test_transferred_heartbeat_keeps_long_finalization_live_with_controlled_time(
    monkeypatch,
):
    """A finalization longer than the 90-second TTL keeps refreshing liveness."""

    from app.jobs import worker_control

    now = {"value": 5.0}
    expirations: list[tuple[float, int, float]] = []

    class ControlledStop:
        def wait(self, interval):
            now["value"] += interval
            return now["value"] > 105.0

        def set(self):
            now["value"] = 106.0

    monkeypatch.setattr(
        worker_control.TaskEventPublisher,
        "publish_heartbeat",
        lambda _job_id, _task_type, *, pid: expirations.append(
            (
                now["value"],
                pid,
                now["value"] + worker_control.TaskEventPublisher.HEARTBEAT_TTL,
            )
        )
        or True,
    )

    heartbeat = worker_control.HeartbeatPublisher(
        "download-job",
        "download",
        pid=4242,
    )
    heartbeat._stop_event = ControlledStop()
    heartbeat.transfer_to_pid(9001)
    heartbeat._run()

    assert expirations[0] == (5.0, 9001, 95.0)
    assert all(pid == 9001 for _, pid, _ in expirations)
    assert max(published_at for published_at, _, _ in expirations) > 90.0
    assert max(expires_at for _, _, expires_at in expirations) > 105.0


@pytest.mark.parametrize("command", ["pause", "cancel"])
def test_finalization_control_is_a_checkpoint_after_child_detach(
    monkeypatch,
    command,
):
    """Finalization observes pause/cancel without signalling the reaped child."""

    from app.jobs import worker_control

    signals: list[tuple[int, int]] = []

    class FakePubSub:
        def subscribe(self, _channel):
            return None

        def listen(self):
            yield {
                "type": "message",
                "data": json.dumps({"command": command, "reason": "operator"}),
            }

        def unsubscribe(self, _channel):
            return None

        def close(self):
            return None

    class FakeRedis:
        def pubsub(self):
            return FakePubSub()

    monkeypatch.setattr(worker_control, "get_redis", lambda: FakeRedis())
    monkeypatch.setattr(
        worker_control,
        "signal_process_group",
        lambda pid, sig: signals.append((pid, sig)) or True,
    )

    listener = worker_control.ControlListener("download-job", proc_pid=4242)
    assert listener.detach_process(4242) is True
    listener._listen()

    assert listener.command == command
    assert listener.reason == "operator"
    assert listener.should_stop() is True
    assert signals == []


def test_process_detach_is_fenced_by_expected_pid(monkeypatch):
    """An obsolete owner cannot detach a newer child process target."""

    from app.jobs import worker_control

    signals: list[int] = []
    monkeypatch.setattr(
        worker_control,
        "signal_process_group",
        lambda pid, _sig: signals.append(pid) or True,
    )

    listener = worker_control.ControlListener("download-job", proc_pid=4242)

    assert listener.detach_process(1111) is False
    listener._handle_cancel()
    assert signals == [4242]
    assert listener.detach_process(4242) is True
    listener._handle_pause()
    assert signals == [4242]


def test_control_listener_stop_closes_blocking_pubsub_and_joins(monkeypatch):
    """A completed job must return its shared-pool connection immediately."""

    from app.jobs import worker_control

    entered = threading.Event()
    closed = threading.Event()

    class BlockingPubSub:
        def subscribe(self, _channel):
            return None

        def listen(self):
            entered.set()
            while not closed.wait(0.01):
                pass
            if False:
                yield None

        def unsubscribe(self, _channel):
            return None

        def close(self):
            closed.set()

    pubsub = BlockingPubSub()

    class FakeRedis:
        def pubsub(self):
            return pubsub

    monkeypatch.setattr(worker_control, "get_redis", lambda: FakeRedis())
    listener = worker_control.ControlListener("completed-import")
    listener.start()
    assert entered.wait(1)

    try:
        listener.stop()

        assert closed.is_set()
        assert listener._thread is not None
        assert not listener._thread.is_alive()
    finally:
        closed.set()
        if listener._thread is not None:
            listener._thread.join(timeout=1)


async def _clear_download_tables(db) -> None:
    from sqlalchemy import text

    await db.execute(
        text(
            "TRUNCATE task_events, task_runs, storage_artifacts, import_jobs, "
            "download_jobs, subscription_sources, subscriptions, creators "
            "RESTART IDENTITY CASCADE"
        )
    )
    await db.commit()


async def _seed_download(db):
    from app.models import Creator, DownloadJob, Subscription

    creator = Creator(name=f"download-liveness-{uuid4().hex}")
    db.add(creator)
    await db.flush()
    subscription = Subscription(creator_id=creator.id, name="Download liveness")
    db.add(subscription)
    await db.flush()
    job = DownloadJob(
        subscription_id=subscription.id,
        source="pixiv",
        source_url="https://localhost/users/123",
        status="enqueued",
    )
    db.add(job)
    await db.commit()
    return job.id


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("handoff_error", [False, True])
async def test_download_keeps_worker_heartbeat_until_durable_import_handoff(
    tmp_path,
    monkeypatch,
    handoff_error,
):
    """The child handoff stays live through scanning and import publication."""

    from app.database import async_session, engine
    from app.jobs import download
    from app.services import job_progress, proxy

    events: list[tuple[str, int | None]] = []

    class FakeProcess:
        pid = 4242
        returncode = 0

        def __init__(self, *_args, **_kwargs):
            self.stdout = StringIO("")
            self.stderr = StringIO("")

        def poll(self):
            return self.returncode

        def wait(self, _timeout=None):
            return self.returncode

    class FakeControl:
        command = None
        stopped = False

        def __init__(self, *_args, **kwargs):
            self.proc_pid = kwargs["proc_pid"]

        def start(self):
            events.append(("control-start", self.proc_pid))

        def detach_process(self, expected_pid):
            assert expected_pid == self.proc_pid
            self.proc_pid = None
            events.append(("control-detach", expected_pid))
            return True

        def stop(self):
            self.stopped = True
            events.append(("control-stop", None))

    class FakeHeartbeat:
        stopped = False

        def __init__(self, *_args, **kwargs):
            self.pid = kwargs["pid"]

        def start(self):
            events.append(("heartbeat-start", self.pid))

        def transfer_to_pid(self, pid):
            self.pid = pid
            events.append(("heartbeat-transfer", pid))
            return True

        def stop(self):
            self.stopped = True
            events.append(("heartbeat-stop", self.pid))

    async def defaults():
        return {"max_posts": 1, "timeout_seconds": 1, "stall_timeout_seconds": 1}

    async def no_proxy():
        return {"enabled": False}

    async def artifact_counts(_job_id):
        return 1, 1, {"pixiv/creator/123.json"}

    async def import_plan(_db, _job, *, metadata_count, metadata_paths):
        assert metadata_count == 1
        return 1, metadata_paths, None

    async def enqueue_import(job_id, import_error=None, new_json_paths=None):
        assert import_error is None
        assert new_json_paths == {"pixiv/creator/123.json"}
        assert events[-1][0] != "heartbeat-stop"
        assert not any(name == "control-stop" for name, _ in events)
        events.append(("import-handoff", None))
        if handoff_error:
            raise RuntimeError("simulated import publication failure")
        return str(uuid4())

    async def no_search_projection(*_args, **_kwargs):
        return None

    raw_runner = download.run_download_job
    while hasattr(raw_runner, "__wrapped__"):
        raw_runner = raw_runner.__wrapped__
    monkeypatch.setattr(download, "_read_download_defaults", defaults)
    monkeypatch.setattr(download, "build_effective_gallerydl_config", lambda *_args: {})
    monkeypatch.setattr(download, "staging_enabled", lambda: False)
    monkeypatch.setattr(download, "ControlListener", FakeControl)
    monkeypatch.setattr(download, "HeartbeatPublisher", FakeHeartbeat)
    monkeypatch.setattr(download.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(download, "_process_group_exists", lambda _pid: False)
    monkeypatch.setattr(download, "_artifact_counts", artifact_counts)
    monkeypatch.setattr(download, "_successful_repository_import_plan", import_plan)
    monkeypatch.setattr(download, "_enqueue_import", enqueue_import)
    monkeypatch.setattr(download, "request_search_projection", no_search_projection)
    monkeypatch.setattr(download.settings, "download_root", str(tmp_path))
    monkeypatch.setattr(proxy, "_load_proxy_config", no_proxy)
    monkeypatch.setattr(job_progress.ProgressTracker, "set", staticmethod(lambda *_a: None))
    monkeypatch.setattr(
        job_progress.TaskEventPublisher,
        "publish_progress",
        staticmethod(lambda *_a: None),
    )
    monkeypatch.setattr(
        download,
        "get_redis",
        lambda: type(
            "Redis",
            (),
            {
                "hset": lambda *_a, **_k: None,
                "expire": lambda *_a, **_k: None,
            },
        )(),
    )

    try:
        async with async_session() as db:
            await _clear_download_tables(db)
            job_id = await _seed_download(db)

        if handoff_error:
            with pytest.raises(RuntimeError, match="simulated import publication failure"):
                await raw_runner(str(job_id))
        else:
            await raw_runner(str(job_id))

        worker_pid = os.getpid()
        assert ("control-detach", 4242) in events
        assert ("heartbeat-transfer", worker_pid) in events
        assert events.index(("control-detach", 4242)) < events.index(
            ("heartbeat-transfer", worker_pid)
        )
        assert events.index(("heartbeat-transfer", worker_pid)) < events.index(
            ("import-handoff", None)
        )
        assert events[-2:] == [
            ("heartbeat-stop", worker_pid),
            ("control-stop", None),
        ]
    finally:
        async with async_session() as db:
            await _clear_download_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["pause", "cancel"])
async def test_download_finalization_checkpoint_prevents_import_handoff(
    tmp_path,
    monkeypatch,
    command,
):
    """A finalization checkpoint honors control after gallery-dl has exited."""

    from app.database import async_session, engine
    from app.jobs import download
    from app.services import job_progress, proxy

    class FakeProcess:
        pid = 4242
        returncode = 0

        def __init__(self, *_args, **_kwargs):
            self.stdout = StringIO("")
            self.stderr = StringIO("")

        def poll(self):
            return self.returncode

        def wait(self, _timeout=None):
            return self.returncode

    class FakeControl:
        command = None

        def __init__(self, *_args, **kwargs):
            self.proc_pid = kwargs["proc_pid"]

        def start(self):
            return None

        def detach_process(self, expected_pid):
            assert expected_pid == self.proc_pid
            self.proc_pid = None
            return True

        def stop(self):
            return None

    listener = None

    def make_control(*args, **kwargs):
        nonlocal listener
        listener = FakeControl(*args, **kwargs)
        return listener

    class FakeHeartbeat:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return None

        def transfer_to_pid(self, _pid):
            return True

        def stop(self):
            return None

    async def defaults():
        return {"max_posts": 1, "timeout_seconds": 1, "stall_timeout_seconds": 1}

    async def no_proxy():
        return {"enabled": False}

    async def artifact_counts(_job_id):
        return 1, 1, {"pixiv/creator/123.json"}

    async def import_plan(_db, _job, *, metadata_count, metadata_paths):
        listener.command = command
        return metadata_count, metadata_paths, None

    async def forbidden_enqueue(*_args, **_kwargs):
        raise AssertionError("paused/cancelled finalization published an import")

    async def no_search_projection(*_args, **_kwargs):
        return None

    raw_runner = download.run_download_job
    while hasattr(raw_runner, "__wrapped__"):
        raw_runner = raw_runner.__wrapped__
    monkeypatch.setattr(download, "_read_download_defaults", defaults)
    monkeypatch.setattr(download, "build_effective_gallerydl_config", lambda *_args: {})
    monkeypatch.setattr(download, "staging_enabled", lambda: False)
    monkeypatch.setattr(download, "ControlListener", make_control)
    monkeypatch.setattr(download, "HeartbeatPublisher", FakeHeartbeat)
    monkeypatch.setattr(download.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(download, "_process_group_exists", lambda _pid: False)
    monkeypatch.setattr(download, "_artifact_counts", artifact_counts)
    monkeypatch.setattr(download, "_successful_repository_import_plan", import_plan)
    monkeypatch.setattr(download, "_enqueue_import", forbidden_enqueue)
    monkeypatch.setattr(download, "request_search_projection", no_search_projection)
    monkeypatch.setattr(download.settings, "download_root", str(tmp_path))
    monkeypatch.setattr(proxy, "_load_proxy_config", no_proxy)
    monkeypatch.setattr(job_progress.ProgressTracker, "set", staticmethod(lambda *_a: None))
    monkeypatch.setattr(
        job_progress.TaskEventPublisher,
        "publish_progress",
        staticmethod(lambda *_a: None),
    )
    monkeypatch.setattr(
        download,
        "get_redis",
        lambda: type(
            "Redis",
            (),
            {
                "hset": lambda *_a, **_k: None,
                "expire": lambda *_a, **_k: None,
            },
        )(),
    )

    try:
        async with async_session() as db:
            await _clear_download_tables(db)
            job_id = await _seed_download(db)

        await raw_runner(str(job_id))
    finally:
        async with async_session() as db:
            await _clear_download_tables(db)
        await engine.dispose()
