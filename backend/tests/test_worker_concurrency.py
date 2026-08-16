import inspect

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
