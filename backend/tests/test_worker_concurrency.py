import inspect
import json
import signal

import pytest

import worker_entrypoint

from worker_entrypoint import (
    RESOURCE_AWARE_WORKER_CLASS,
    RESTART_CIRCUIT_SECONDS,
    RestartCircuit,
    _worker_command,
    build_worker_specs,
    resolve_concurrency,
    resolve_download_concurrency,
)


def test_prefers_db_value_over_argv():
    assert resolve_concurrency(4, "3") == 4


def test_clamps_above_max():
    assert resolve_concurrency(9, "3") == 5


def test_clamps_below_min():
    assert resolve_concurrency(0, "3") == 1
    assert resolve_concurrency(-2, None) == 1


def test_falls_back_to_argv_when_db_missing():
    assert resolve_concurrency(None, "2") == 2


def test_falls_back_to_default_when_both_invalid():
    assert resolve_concurrency(None, None) == 3
    assert resolve_concurrency("x", "y") == 3


def test_deployment_cap_limits_effective_download_concurrency(monkeypatch):
    monkeypatch.setenv("DOWNLOAD_CONCURRENCY_CAP", "1")

    assert resolve_download_concurrency(4, "3") == (4, 1, 1)


def test_every_worker_uses_resource_aware_class():
    command = _worker_command(["imports"], with_scheduler=True)

    assert command[command.index("-w") + 1] == RESOURCE_AWARE_WORKER_CLASS
    assert "--with-scheduler" in command


def test_extra_maintenance_listener_owns_its_scheduled_registry():
    specs = build_worker_specs(
        ["imports"],
        1,
        with_scheduler=True,
        extra_queues=["maintenance"],
    )

    assert specs == [(('imports',), True), (('maintenance',), True)]


def test_restart_preserves_each_queue_scoped_scheduler():
    source = inspect.getsource(worker_entrypoint.main)

    assert "scheduler=pending.with_scheduler" in source
    assert "any(item.with_scheduler" not in source


def test_restart_backoff_and_circuit_breaker():
    circuit = RestartCircuit()

    assert [circuit.record_exit(i * 10) for i in range(4)] == [5.0, 15.0, 30.0, 60.0]
    assert circuit.record_exit(40) == RESTART_CIRCUIT_SECONDS
    assert circuit.is_open(41) is True
    assert circuit.is_open(40 + RESTART_CIRCUIT_SECONDS) is False


@pytest.mark.parametrize("unresponsive", [False, True, "delayed_reap"])
def test_supervisor_reports_actual_child_exit_and_forced_termination(monkeypatch, capsys, unresponsive):
    """A successful supervisor exit must not disguise a child killed at 55s."""
    handlers = {}
    clock = [0.0]
    signalled = [False]

    class Process:
        pid = 4242
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            if not unresponsive:
                self.returncode = 0

        kills = 0

        def kill(self):
            self.kills += 1
            if unresponsive != "delayed_reap" or self.kills > 1:
                self.returncode = -signal.SIGKILL

        def wait(self, timeout):
            if self.returncode is None:
                raise worker_entrypoint.subprocess.TimeoutExpired("owned-worker", timeout)
            return self.returncode

    def sleep(seconds):
        if not signalled[0]:
            signalled[0] = True
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        else:
            clock[0] += seconds

    monkeypatch.setattr(worker_entrypoint.sys, "argv", ["worker_entrypoint.py", "shutdown-test", "1"])
    monkeypatch.delenv("WORKER_EXTRA_QUEUES", raising=False)
    monkeypatch.setattr(worker_entrypoint, "_register_resource_state_bridge", lambda: None)
    monkeypatch.setattr(worker_entrypoint, "_sweep_personal_auth_startup", lambda _: 0)
    monkeypatch.setattr(worker_entrypoint, "_publish_supervisor_status", lambda *_: None)
    monkeypatch.setattr(worker_entrypoint.subprocess, "Popen", lambda _: Process())
    monkeypatch.setattr(worker_entrypoint.signal, "signal", lambda key, callback: handlers.update({key:callback}))
    monkeypatch.setattr(worker_entrypoint.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker_entrypoint.time, "sleep", sleep)
    worker_entrypoint.main()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    stopped = [event for event in events if event.get("event") == "worker_stopped"]
    assert stopped == [{"event":"worker_stopped", "pid":4242,
                        "queues":["shutdown-test"],
                        "return_code":-signal.SIGKILL if unresponsive else 0, "forced":bool(unresponsive)}]
    assert [event for event in events if event.get("event") == "worker_started"] == [
        {"event":"worker_started", "pid":4242, "queues":["shutdown-test"]}
    ]
