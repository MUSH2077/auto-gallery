"""Low-overhead resource accounting for bounded pipeline stages.

The counters are process-local and context-scoped.  They add no database
writes: one structured log record is emitted when a stage ends, while global
SQLAlchemy hooks merely increment integers when a measured context is active.
``asyncio.to_thread`` copies context variables, so libvips/ffmpeg orchestration
and Meilisearch SDK calls remain attributed to their initiating micro-batch.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import logging
import os
import resource
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.database import engine

logger = logging.getLogger(__name__)


@dataclass
class _ImportMetrics:
    job_id: str
    execution_token: str
    seconds: dict[str, float] = field(default_factory=dict)
    deferred: dict[str, int] = field(default_factory=dict)


_import_metrics: contextvars.ContextVar[_ImportMetrics | None] = contextvars.ContextVar(
    "import_execution_metrics", default=None,
)


@contextlib.contextmanager
def import_execution_metrics(job_id, execution_token, *, queue_wait_seconds=0.0):
    """Keep attempt timings separate, including when an older attempt exits late."""
    metrics = _ImportMetrics(str(job_id), str(execution_token))
    metrics.seconds["queue_wait"] = max(0.0, queue_wait_seconds)
    token = _import_metrics.set(metrics)
    try:
        yield metrics
    finally:
        payload = {
            "stage": "import_execution",
            "job_id": metrics.job_id,
            "execution_token": metrics.execution_token,
            "phase_timings_ms": {key: round(value * 1000) for key, value in metrics.seconds.items()},
            "deferred_work": dict(metrics.deferred),
        }
        _import_metrics.reset(token)
        _log_metrics(payload)


@contextlib.contextmanager
def measure_import_phase(phase: str):
    metrics = _import_metrics.get()
    if metrics is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        metrics.seconds[phase] = metrics.seconds.get(phase, 0.0) + time.perf_counter() - started


def timed_import_phase(phase: str):
    def decorate(func):
        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            with measure_import_phase(phase):
                return await func(*args, **kwargs)
        return wrapped
    return decorate


def import_metrics_progress(job) -> dict[str, Any]:
    metrics = _import_metrics.get()
    if metrics is None or str(job.id) != metrics.job_id or str(getattr(job, "execution_token", None)) != metrics.execution_token:
        return {}
    return {
        "execution_token": metrics.execution_token,
        "phase_timings_ms": {key: round(value * 1000) for key, value in metrics.seconds.items()},
        "deferred_work": dict(metrics.deferred),
    }


def record_import_deferred(**counts: int) -> None:
    """Record only intents whose domain/outbox transaction has committed."""
    metrics = _import_metrics.get()
    if metrics is not None:
        for key, value in counts.items():
            metrics.deferred[key] = metrics.deferred.get(key, 0) + value


def _log_metrics(payload: dict[str, Any]) -> None:
    # Worker/RQ formatters emit %(message)s, so `extra` alone loses every value.
    logger.info("pipeline stage metrics " + json.dumps(payload, default=str, separators=(",", ":")), extra={"stage_metrics": payload})


@dataclass
class _Counters:
    sql: int = 0
    commits: int = 0


_active: contextvars.ContextVar[_Counters | None] = contextvars.ContextVar(
    "pipeline_stage_metrics",
    default=None,
)
_hooks_installed = False
_hooks_lock = threading.Lock()


def _install_hooks() -> None:
    global _hooks_installed
    if _hooks_installed:
        return
    with _hooks_lock:
        if _hooks_installed:
            return

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _count_sql(*_args, **_kwargs) -> None:
            counters = _active.get()
            if counters is not None:
                counters.sql += 1

        @event.listens_for(Session, "after_commit")
        def _count_commit(_session) -> None:
            counters = _active.get()
            if counters is not None and not _session.in_nested_transaction():
                counters.commits += 1

        _hooks_installed = True


def _process_io() -> tuple[int | None, int | None]:
    try:
        fields: dict[str, int] = {}
        for line in Path("/proc/self/io").read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition(":")
            if separator:
                fields[key] = int(value.strip())
        return fields.get("read_bytes"), fields.get("write_bytes")
    except (OSError, ValueError):
        return None, None


def _rss_peak_bytes() -> int:
    # Linux reports KiB, macOS bytes.  Production is Linux; retaining the small
    # portability branch keeps local unit tests meaningful.
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value * 1024 if os.name == "posix" and Path("/proc/self").exists() else value


@contextlib.contextmanager
def measure_stage(
    stage: str,
    **dimensions: Any,
) -> Iterator[dict[str, Any]]:
    """Measure one stage and expose its final payload to the caller."""

    _install_hooks()
    counters = _Counters()
    token = _active.set(counters)
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    read_started, write_started = _process_io()
    rss_started = _rss_peak_bytes()
    payload: dict[str, Any] = {
        "stage": stage,
        **{key: value for key, value in dimensions.items() if value is not None},
    }
    execution = _import_metrics.get()
    if execution is not None:
        payload.update(job_id=execution.job_id, execution_token=execution.execution_token)
    outcome = "ok"
    try:
        yield payload
    except BaseException:
        outcome = "error"
        raise
    finally:
        read_finished, write_finished = _process_io()
        payload.update(
            outcome=outcome,
            wall_seconds=round(time.perf_counter() - wall_started, 6),
            cpu_seconds=round(time.process_time() - cpu_started, 6),
            read_bytes=(
                max(0, read_finished - read_started)
                if read_started is not None and read_finished is not None
                else None
            ),
            write_bytes=(
                max(0, write_finished - write_started)
                if write_started is not None and write_finished is not None
                else None
            ),
            sql_count=counters.sql,
            commit_count=counters.commits,
            rss_peak_bytes=max(rss_started, _rss_peak_bytes()),
        )
        _active.reset(token)
        parent = _active.get()
        if parent is not None:
            parent.sql += counters.sql
            parent.commits += counters.commits
        _log_metrics(payload)


class measure_async_stage:
    """Async facade over :func:`measure_stage` without another task/thread."""

    def __init__(self, stage: str, **dimensions: Any) -> None:
        self._manager = measure_stage(stage, **dimensions)

    async def __aenter__(self) -> dict[str, Any]:
        return self._manager.__enter__()

    async def __aexit__(self, exc_type, exc, traceback) -> bool | None:
        return self._manager.__exit__(exc_type, exc, traceback)


def measured_async_job(stage: str):
    """Decorate an async job without changing its RQ import path/signature."""

    def decorate(func):
        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            async with measure_async_stage(stage):
                return await func(*args, **kwargs)

        return wrapped

    return decorate
