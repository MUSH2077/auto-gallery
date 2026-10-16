"""Optional forkserver consumers; task execution remains in RQ workhorses."""
from __future__ import annotations

import multiprocessing
import os
import select
import signal
import subprocess


def _run_rq(arguments: list[str], environment: dict[str, str], ready) -> None:
    # Apply resolved per-consumer values before importing app.config or RQ's
    # resource worker. The template contains definitions, never live clients.
    os.environ.update(environment)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    from app.services import worker_preload

    if worker_preload.PRELOAD_PID == os.getpid():
        raise RuntimeError("Shared worker initialization was not inherited")
    ready.send(worker_preload.PRELOAD_PID)
    ready.close()
    # Invoke the same Click command in this interpreter. exec/rq subprocesses
    # would discard the shared initialization and change the memory result.
    from rq.cli import main

    main(args=arguments, prog_name="rq")


def follow_consumer_lifetime(consumer_pid: int) -> None:
    """Stop the shared consumer's RQ scheduler when its parent disappears."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    libc.prctl.restype = ctypes.c_int
    # PR_SET_PDEATHSIG is Linux-specific, like this optional shared mode.
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    # The consumer may have died between fork and installing this signal.
    if os.getppid() != consumer_pid:
        os.kill(os.getpid(), signal.SIGTERM)


class SharedWorkerProcess:
    """Popen-shaped process handle whose liveness survives forkserver loss.

    multiprocessing's status pipe can report exit when its forkserver dies
    while a consumer is still alive. Linux pidfds track the actual process,
    including during graceful shutdown, without risking PID reuse.
    """

    def __init__(self, process):
        self._process = process
        self.pid = process.pid
        self._pidfd = os.pidfd_open(self.pid)

    def poll(self):
        if not select.select([self._pidfd], [], [], 0)[0]:
            return None
        self._process.join(timeout=0)
        return self._process.exitcode

    def wait(self, timeout=None):
        if not select.select([self._pidfd], [], [], timeout)[0]:
            raise subprocess.TimeoutExpired("shared RQ worker", timeout)
        self._process.join(timeout=timeout)
        if self._process.exitcode is None:
            raise subprocess.TimeoutExpired("shared RQ worker status", timeout)
        return self._process.exitcode

    def _signal(self, value):
        try:
            signal.pidfd_send_signal(self._pidfd, value)
        except ProcessLookupError:
            pass

    def terminate(self):
        self._signal(signal.SIGTERM)

    def kill(self):
        self._signal(signal.SIGKILL)

    def __del__(self):
        fd = getattr(self, "_pidfd", None)
        if fd is not None:
            os.close(fd)


class SharedWorkerFactory:
    def __init__(self):
        # Explicit local context: RQ's own scheduler/workhorse fork mechanism
        # and other services retain their existing multiprocessing defaults.
        self._context = multiprocessing.get_context("forkserver")
        multiprocessing.set_forkserver_preload(["app.services.worker_preload"])
        self.template_pid = None
        self._template_pidfd = None

    def start(self, command: list[str], environment: dict[str, str] | None = None):
        if not self.alive():
            raise RuntimeError("Shared initialization process exited")
        read, write = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=_run_rq, args=(command[1:], environment or {}, write), daemon=False,
        )
        handle = None
        try:
            process.start()
            handle = SharedWorkerProcess(process)
            write.close()
            if not read.poll(15):
                raise RuntimeError("Shared worker initialization timed out")
            template_pid = read.recv()
            if self.template_pid is None:
                self.template_pid = template_pid
                self._template_pidfd = os.pidfd_open(template_pid)
            elif template_pid != self.template_pid:
                raise RuntimeError("Shared initialization process changed unexpectedly")
            return handle
        except BaseException:
            if handle is not None:
                handle.kill()
                handle.wait(timeout=10)
            elif process.pid is not None:
                process.kill()
                process.join(timeout=10)
            raise
        finally:
            read.close()
            write.close()

    def alive(self):
        return self._template_pidfd is None or not select.select(
            [self._template_pidfd], [], [], 0,
        )[0]

    def close(self):
        if self._template_pidfd is not None:
            os.close(self._template_pidfd)
            self._template_pidfd = None
