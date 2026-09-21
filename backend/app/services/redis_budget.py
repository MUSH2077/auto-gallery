"""Short Redis deadlines for bounded batch work; ordinary clients are unchanged."""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import redis
from redis.backoff import NoBackoff
from redis.client import Pipeline
from redis.exceptions import TimeoutError
from redis.retry import Retry

from app.config import settings


class _BudgetPipeline(Pipeline):
    def __init__(self, *args, deadline, **kwargs):
        self.deadline = deadline
        super().__init__(*args, **kwargs)

    def execute(self, *args, **kwargs):
        _check_deadline(self.deadline)
        return super().execute(*args, **kwargs)

    def execute_command(self, *args, **kwargs):
        _check_deadline(self.deadline)
        return super().execute_command(*args, **kwargs)


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError("Scheduler Redis I/O deadline exhausted; retry durable outbox")


class _BudgetRedis(redis.Redis):
    def __init__(self, *, deadline, **kwargs):
        self.deadline = deadline
        super().__init__(**kwargs)

    def execute_command(self, *args, **kwargs):
        _check_deadline(self.deadline)
        return super().execute_command(*args, **kwargs)

    def pipeline(self, transaction=True, shard_hint=None):
        return _BudgetPipeline(self.connection_pool, self.response_callbacks, transaction, shard_hint, deadline=self.deadline)


@dataclass(frozen=True)
class RedisBudget:
    client: redis.Redis
    deadline: float
    work_deadline: float


_BUDGET: ContextVar[RedisBudget | None] = ContextVar("scheduler_redis_budget", default=None)


def current_budget_client():
    budget = _BUDGET.get()
    return budget.client if budget else None


def current_work_deadline():
    budget = _BUDGET.get()
    return budget.work_deadline if budget else time.monotonic() + 17


@contextmanager
def budget_redis(*, seconds=20, reserve_seconds=3, url=None):
    """Share one absolute deadline across helpers and asyncio.to_thread calls.

    Reserve lets in-flight atomic publication settle while holding its parent
    fence. Commands and pipelines check the absolute deadline; each socket
    wait is at most 0.5s and has no implicit retries. Nested dispatch helpers
    keep the outer deadline instead of extending the worker's time budget.
    """
    existing = _BUDGET.get()
    if existing is not None:
        yield existing
        return
    started = time.monotonic()
    deadline = started + max(0.01, seconds)
    pool = redis.ConnectionPool.from_url(
        url or settings.redis_url,
        max_connections=4,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
        retry_on_timeout=False,
        retry=Retry(NoBackoff(), 0),
        health_check_interval=0,
        socket_keepalive=True,
        decode_responses=False,
    )
    client = _BudgetRedis(connection_pool=pool, deadline=deadline)
    budget = RedisBudget(client, deadline, started + max(0, seconds - reserve_seconds))
    token = _BUDGET.set(budget)
    try:
        yield budget
    finally:
        _BUDGET.reset(token)
        client.close()
        pool.disconnect()
