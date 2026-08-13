import signal
import subprocess

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
