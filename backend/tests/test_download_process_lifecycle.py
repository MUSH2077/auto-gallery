import signal
import subprocess
import sys
import time

import pytest

from app.jobs import download


class _FakeProcess:
    pid = 4242

    def __init__(self):
        self.returncode = None
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.wait_calls += 1
        if self.wait_calls == 1:
            raise subprocess.TimeoutExpired("gallery-dl", timeout)
        self.returncode = -signal.SIGKILL
        return self.returncode


def test_wait_gallerydl_process_reaps_short_lived_child_without_poll_interval_delay():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.05)"],
        start_new_session=True,
    )
    started = time.monotonic()
    try:
        assert download._wait_gallerydl_process(proc, timeout=5.0) is True
        assert time.monotonic() - started < 1.0
        assert proc.poll() == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=1.0)


def test_wait_gallerydl_process_reports_timeout_while_child_is_alive_then_cleans_up():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        start_new_session=True,
    )
    try:
        assert download._wait_gallerydl_process(proc, timeout=0.2) is False
        assert proc.poll() is None
    finally:
        download._stop_gallerydl_process(
            proc,
            initial_signal=signal.SIGTERM,
            graceful_timeout=1.0,
            kill_timeout=1.0,
        )
        assert proc.poll() is not None


def test_wait_gallerydl_process_uses_bounded_wait_and_returns_on_exit():
    calls = []

    class _CompletedProcess:
        def wait(self, timeout):
            calls.append(timeout)
            return 0

    proc = _CompletedProcess()

    assert download._wait_gallerydl_process(proc, timeout=5.0) is True
    assert calls == [5.0]


def test_wait_gallerydl_process_reports_poll_timeout():
    class _RunningProcess:
        def wait(self, timeout):
            raise subprocess.TimeoutExpired("gallery-dl", timeout)

    assert download._wait_gallerydl_process(_RunningProcess(), timeout=5.0) is False


def test_stop_gallerydl_escalates_and_reaps(monkeypatch):
    proc = _FakeProcess()
    signals = []
    monkeypatch.setattr(
        download,
        "signal_process_group",
        lambda pid, sig: signals.append((pid, sig)) or True,
    )
    monkeypatch.setattr(download, "_process_group_exists", lambda _pgid: False)

    returncode = download._stop_gallerydl_process(
        proc,
        graceful_timeout=0.01,
        kill_timeout=0.01,
    )

    assert returncode == -signal.SIGKILL
    assert signals == [
        (proc.pid, signal.SIGTERM),
        (proc.pid, signal.SIGKILL),
    ]
    assert proc.poll() is not None


def test_stop_gallerydl_fails_if_process_group_survives(monkeypatch):
    proc = _FakeProcess()
    proc.returncode = 0
    monkeypatch.setattr(download, "_process_group_exists", lambda _pgid: True)
    monkeypatch.setattr(download.os, "killpg", lambda _pgid, _sig: None)
    monkeypatch.setattr(download.time, "sleep", lambda _seconds: None)
    ticks = iter((0.0, 1.0, 2.0, 3.0))
    monkeypatch.setattr(download.time, "monotonic", lambda: next(ticks, 4.0))

    with pytest.raises(RuntimeError, match="survived SIGKILL"):
        download._stop_gallerydl_process(
            proc,
            graceful_timeout=0.01,
            kill_timeout=0.5,
        )
