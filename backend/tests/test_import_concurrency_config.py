import os
import signal

import pytest

import worker_entrypoint


class FakeProcess:
    def __init__(self, *, alive=True):
        self.returncode = None if alive else 1

    def poll(self):
        return self.returncode


def test_import_concurrency_defaults_to_one(monkeypatch):
    monkeypatch.delenv('IMPORT_WORKER_CONCURRENCY', raising=False)

    assert worker_entrypoint.resolve_import_concurrency(None) == 1


def test_import_concurrency_uses_environment_when_cli_is_absent(monkeypatch):
    monkeypatch.setenv('IMPORT_WORKER_CONCURRENCY', '3')

    assert worker_entrypoint.resolve_import_concurrency(None) == 3


def test_import_cli_value_overrides_environment(monkeypatch):
    monkeypatch.setenv('IMPORT_WORKER_CONCURRENCY', '2')

    assert worker_entrypoint.resolve_import_concurrency('4') == 4


def test_invalid_cli_value_is_not_hidden_by_valid_environment(monkeypatch):
    monkeypatch.setenv('IMPORT_WORKER_CONCURRENCY', '2')

    with pytest.raises(ValueError, match=r'CLI IMPORT_WORKER_CONCURRENCY'):
        worker_entrypoint.resolve_import_concurrency('0')


@pytest.mark.parametrize('value', ['0', '5', 'x', ''])
def test_import_invalid_environment_values_raise_clear_error(monkeypatch, value):
    monkeypatch.setenv('IMPORT_WORKER_CONCURRENCY', value)

    with pytest.raises(ValueError, match='IMPORT_WORKER_CONCURRENCY'):
        worker_entrypoint.resolve_import_concurrency(None)


def test_import_queue_counts_separate_maintenance_and_ignore_exited_processes():
    specs = worker_entrypoint.build_worker_specs(
        ['imports'], 2, with_scheduler=True, extra_queues=['maintenance']
    )
    processes = [
        worker_entrypoint.ManagedProcess(FakeProcess(), ('imports',)),
        worker_entrypoint.ManagedProcess(FakeProcess(alive=False), ('imports',)),
        worker_entrypoint.ManagedProcess(FakeProcess(), ('maintenance',)),
    ]

    assert worker_entrypoint._queue_worker_counts(processes, specs) == {
        'imports': {'alive': 1, 'target': 2},
        'maintenance': {'alive': 1, 'target': 1},
    }


def test_import_queue_targets_keep_maintenance_at_one_when_imports_scale():
    specs = worker_entrypoint.build_worker_specs(
        ['imports'], 4, with_scheduler=True, extra_queues=['maintenance']
    )

    assert worker_entrypoint._queue_worker_counts([], specs) == {
        'imports': {'alive': 0, 'target': 4},
        'maintenance': {'alive': 0, 'target': 1},
    }



def test_import_main_cli_value_reaches_bridge_and_child_environment(monkeypatch):
    monkeypatch.setenv('IMPORT_WORKER_CONCURRENCY', '5')
    monkeypatch.setenv('WORKER_EXTRA_QUEUES', 'maintenance')
    monkeypatch.setattr(
        worker_entrypoint.sys,
        'argv',
        ['worker_entrypoint.py', 'imports', '4', '--with-scheduler'],
    )

    bridge_values = []
    popen_calls = []
    handlers = {}
    shutdown_requested = False

    monkeypatch.setattr(
        worker_entrypoint,
        '_register_resource_state_bridge',
        lambda: bridge_values.append(os.environ['IMPORT_WORKER_CONCURRENCY']),
    )
    monkeypatch.setattr(worker_entrypoint, '_sweep_personal_auth_startup', lambda _: 0)
    monkeypatch.setattr(worker_entrypoint, '_publish_supervisor_status', lambda *_: None)

    class Process:
        next_pid = 1000

        def __init__(self):
            self.pid = Process.next_pid
            Process.next_pid += 1
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -signal.SIGKILL

        def wait(self, timeout):
            return self.returncode

    def popen(command, env=None):
        process = Process()
        popen_calls.append((command, env))
        return process

    monkeypatch.setattr(worker_entrypoint.subprocess, 'Popen', popen)
    monkeypatch.setattr(
        worker_entrypoint.signal,
        'signal',
        lambda key, callback: handlers.__setitem__(key, callback),
    )

    def sleep(_seconds):
        nonlocal shutdown_requested
        if not shutdown_requested:
            shutdown_requested = True
            handlers[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr(worker_entrypoint.time, 'sleep', sleep)

    worker_entrypoint.main()

    assert bridge_values == ['4']
    import_calls = [
        (command, env)
        for command, env in popen_calls
        if 'imports' in command
    ]
    maintenance_calls = [
        (command, env)
        for command, env in popen_calls
        if 'maintenance' in command
    ]
    assert len(import_calls) == 4
    assert len(maintenance_calls) == 1
    assert all(env['IMPORT_WORKER_CONCURRENCY'] == '4' for _, env in import_calls)
    # The parent environment is also normalized before the maintenance child
    # is spawned, so its Settings import remains valid in the shared container.
    assert os.environ['IMPORT_WORKER_CONCURRENCY'] == '4'



def test_import_main_rejects_invalid_environment_before_config_bridge(monkeypatch):
    monkeypatch.setenv('IMPORT_WORKER_CONCURRENCY', '5')
    monkeypatch.delenv('WORKER_EXTRA_QUEUES', raising=False)
    monkeypatch.setattr(
        worker_entrypoint.sys,
        'argv',
        ['worker_entrypoint.py', 'imports', '--with-scheduler'],
    )
    bridge_called = []
    monkeypatch.setattr(
        worker_entrypoint,
        '_register_resource_state_bridge',
        lambda: bridge_called.append(True),
    )

    with pytest.raises(SystemExit) as exc_info:
        worker_entrypoint.main()

    assert exc_info.value.code == 2
    assert bridge_called == []
