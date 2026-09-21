"""Cross-process invalidation generation for the cached workbench summary."""

from __future__ import annotations

import logging

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.services.redis_client import get_redis


logger = logging.getLogger(__name__)

WORKBENCH_GENERATION_KEY = "cache:workbench:generation:v1"
_INVALIDATION_INTENTS = "workbench_invalidation_intents"
_INVALIDATION_COMMITTING = "workbench_invalidation_committing"


def read_workbench_generation() -> int | None:
    """Return the shared generation, or None when Redis is unavailable."""

    try:
        from app.services.redis_budget import budget_redis

        with budget_redis(seconds=0.25, reserve_seconds=0):
            value = get_redis().get(WORKBENCH_GENERATION_KEY)
        if value is None:
            return 0
        return int(value)
    except Exception:
        logger.debug("Workbench cache generation unavailable", exc_info=True)
        return None


def bump_workbench_generation(redis_client=None) -> int | None:
    """Invalidate every HTTP process after a committed task event."""

    try:
        if redis_client is not None:
            return int(redis_client.incr(WORKBENCH_GENERATION_KEY))
        from app.services.redis_budget import budget_redis

        with budget_redis(seconds=0.25, reserve_seconds=0):
            return int(get_redis().incr(WORKBENCH_GENERATION_KEY))
    except Exception:
        logger.debug("Workbench cache generation bump failed", exc_info=True)
        return None


def mark_workbench_invalidation_pending(db: AsyncSession) -> None:
    """Stage invalidation on the transaction that owns a task event."""

    session = getattr(db, "sync_session", db)
    info = getattr(session, "info", None)
    if not isinstance(info, dict):
        return
    get_transaction = getattr(session, "get_transaction", None)
    get_nested_transaction = getattr(session, "get_nested_transaction", None)
    transaction = (
        (get_nested_transaction() if callable(get_nested_transaction) else None)
        or (get_transaction() if callable(get_transaction) else None)
        or "outer"
    )
    info.setdefault(_INVALIDATION_INTENTS, {})[transaction] = True


@event.listens_for(Session, "before_commit")
def _arm_workbench_invalidation(session: Session) -> None:
    if session.in_nested_transaction():
        return
    intents = session.info.get(_INVALIDATION_INTENTS, {})
    transaction = session.get_transaction()
    if intents.get(transaction) or intents.get("outer"):
        session.info[_INVALIDATION_COMMITTING] = True


@event.listens_for(Session, "after_commit")
def _publish_workbench_invalidation(session: Session) -> None:
    intents = session.info.get(_INVALIDATION_INTENTS, {})
    nested = session.get_nested_transaction()
    if nested is not None:
        if intents.pop(nested, False):
            intents[nested.parent] = True
        return
    armed = session.info.pop(_INVALIDATION_COMMITTING, False)
    session.info.pop(_INVALIDATION_INTENTS, None)
    if armed:
        bump_workbench_generation()


@event.listens_for(Session, "after_soft_rollback")
def _discard_workbench_invalidation(session: Session, previous_transaction) -> None:
    intents = session.info.get(_INVALIDATION_INTENTS, {})
    if previous_transaction.parent is None:
        session.info.pop(_INVALIDATION_INTENTS, None)
        session.info.pop(_INVALIDATION_COMMITTING, None)
    else:
        intents.pop(previous_transaction, None)
