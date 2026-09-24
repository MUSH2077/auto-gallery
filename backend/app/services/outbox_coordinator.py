"""Low-write RQ wake-ups for PostgreSQL-authoritative outboxes."""

from __future__ import annotations

import logging
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic

from rq import Queue
from sqlalchemy import and_, delete, event, exists, func, literal, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, aliased

from app.config import settings
from app.models import (
    AssetDedupOutbox,
    CurationCommit,
    GitlleryProjectionOutbox,
    GitlleryProjectionTarget,
    GitlleryRepositoryState,
    ImportCurationOutbox,
    MediaDerivativeOutbox,
)
from app.models.search_projection_outbox import SearchProjectionOutbox
from app.services.queue_admission import (
    QueueAdmissionError,
    checked_enqueue,
    checked_enqueue_in,
)
from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)

_CLEAR_WAKE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""

_REPLACE_WAKE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  redis.call('set', KEYS[1], ARGV[2])
  return 1
end
return 0
"""

# Wake markers deliberately have no TTL.  A queued coordinator may wait for
# hours in critical mode; expiring the marker would enqueue one duplicate on
# every health tick.  Crash recovery instead verifies the referenced RQ job's
# durable status and compare-deletes only stale/terminal owners.
WAKE_DEBOUNCE_SECONDS = 0

_ACTIVE_STATES = ("pending", "failed", "processing")
OUTBOX_HEALTH_CACHE_TTL_SECONDS = 30.0
OUTBOX_HEALTH_REDIS_TTL_SECONDS = 90
OUTBOX_HEALTH_REDIS_KEY = "outbox:health:v1"
_OUTBOX_WAKE_INTENTS = "outbox_wake_intents"
_OUTBOX_WAKE_COMMITTING = "outbox_wake_committing"
_outbox_health_cache: dict[str, dict] | None = None
_outbox_health_cache_ts = 0.0
_outbox_health_cache_mode: str | None = None
_published_outbox_health: dict[str, dict] | None = None
_SPECS = {
    "import_projection": (
        "app.jobs.import_projection.run_import_projection_outbox",
        (25, 20.0),
    ),
    "media": (
        "app.jobs.media_derivatives.run_media_derivative_outbox",
        (25, 20.0),
    ),
    "gitllery": (
        "app.jobs.gitllery_projection.run_gitllery_projection_outbox",
        (25, 20.0),
    ),
    "search": ("app.jobs.search_projection.run_search_projection_outbox", (500,)),
    "dedup": ("app.jobs.asset_dedup.run_asset_dedup_outbox", (25,)),
}


def _projection_mode() -> str:
    return settings.gitllery_projection_mode.strip().lower()


def _leased_ready(model, now: datetime):
    return or_(
        and_(
            model.state.in_(("pending", "failed")),
            model.available_at <= now,
        ),
        and_(
            model.state == "processing",
            model.lease_expires_at.is_not(None),
            model.lease_expires_at < now,
        ),
    )


def _import_ready(now: datetime):
    import_ready = or_(
        _leased_ready(ImportCurationOutbox, now),
        and_(
            ImportCurationOutbox.metadata_state.in_(("pending", "failed")),
            ImportCurationOutbox.metadata_available_at <= now,
        ),
        and_(
            ImportCurationOutbox.metadata_state == "processing",
            ImportCurationOutbox.metadata_lease_expires_at.is_not(None),
            ImportCurationOutbox.metadata_lease_expires_at < now,
        ),
    )
    return import_ready


def _gitllery_ready(now: datetime):
    """Build Gitllery readiness only after active mode is explicitly selected."""

    git_parent = aliased(GitlleryProjectionOutbox)
    git_head = (
        select(
            GitlleryProjectionOutbox.state.label("state"),
            GitlleryProjectionOutbox.available_at.label("available_at"),
            GitlleryProjectionOutbox.lease_expires_at.label("lease_expires_at"),
        )
        .join(
            CurationCommit,
            CurationCommit.id == GitlleryProjectionOutbox.commit_id,
        )
        .outerjoin(
            git_parent,
            git_parent.commit_id == CurationCommit.parent_commit_id,
        )
        .where(
            GitlleryProjectionOutbox.state != "complete",
            or_(
                CurationCommit.parent_commit_id.is_(None),
                git_parent.id.is_(None),
                git_parent.state == "complete",
            ),
        )
        .order_by(CurationCommit.created_at, CurationCommit.id)
        .limit(1)
        .subquery()
    )
    git_head_ready = or_(
        and_(
            git_head.c.state.in_(("pending", "failed")),
            git_head.c.available_at <= now,
        ),
        and_(
            git_head.c.state == "processing",
            git_head.c.lease_expires_at.is_not(None),
            git_head.c.lease_expires_at < now,
        ),
    )
    earlier_target = aliased(GitlleryProjectionTarget)
    target_has_earlier = exists(
        select(earlier_target.id).where(
            earlier_target.repository_key
            == GitlleryProjectionTarget.repository_key,
            earlier_target.state != "complete",
            tuple_(
                earlier_target.commit_created_at,
                earlier_target.commit_id,
            )
            < tuple_(
                GitlleryProjectionTarget.commit_created_at,
                GitlleryProjectionTarget.commit_id,
            ),
        )
    )
    git_target_ready = and_(
        ~target_has_earlier,
        _leased_ready(GitlleryProjectionTarget, now),
    )
    return or_(
        exists(select(1).select_from(git_head).where(git_head_ready)),
        exists().where(git_target_ready),
    )


def _search_ready(now: datetime):
    from app.models.repository_sync_receipt import SearchIndexState
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    from app.models.search_rebuild import SearchRebuild
    from app.services.search_delivery import checkpoint_due_condition
    from app.services.search_rebuild import membership_bootstrap_due_condition

    active_delivery = exists().where(
        SearchDeliveryReceipt.state.not_in(("complete", "failed"))
    )
    return or_(
        and_(~active_delivery, membership_bootstrap_due_condition()),
        exists().where(SearchRebuild.state.not_in(("complete", "failed"))),
        and_(
            ~active_delivery,
            exists().where(
                SearchProjectionOutbox.completed_at.is_(None),
                SearchProjectionOutbox.available_at <= now,
                or_(
                    SearchProjectionOutbox.lease_until.is_(None),
                    SearchProjectionOutbox.lease_until <= now,
                ),
            ),
        ),
        and_(
            ~active_delivery,
            exists().where(checkpoint_due_condition(SearchIndexState)),
        ),
        exists().where(
            SearchDeliveryReceipt.state.not_in(
                ("complete", "failed", "ambiguous")
            ),
            SearchDeliveryReceipt.available_at <= now,
            or_(
                SearchDeliveryReceipt.lease_until.is_(None),
                SearchDeliveryReceipt.lease_until <= now,
            ),
        ),
    )


def outbox_readiness_statement(now: datetime | None = None):
    """Return one indexed ``EXISTS`` probe for each durable queue.

    Shadow mode deliberately substitutes a SQL literal for Gitllery before any
    Gitllery join or subquery is constructed.  This keeps the ordinary fallback
    independent of both the 70k captured intents and repository metadata.
    """

    now = now or datetime.now(timezone.utc)
    gitllery_ready = (
        _gitllery_ready(now) if _projection_mode() == "active" else literal(False)
    )
    dedup_ready = or_(
        and_(
            AssetDedupOutbox.state.in_(("pending", "failed")),
            AssetDedupOutbox.available_at <= now,
        ),
        and_(
            AssetDedupOutbox.state == "processing",
            AssetDedupOutbox.updated_at <= now - timedelta(minutes=15),
        ),
    )
    return select(
        exists().where(_import_ready(now)).label("import_projection"),
        exists().where(_leased_ready(MediaDerivativeOutbox, now)).label("media"),
        gitllery_ready.label("gitllery"),
        exists().where(dedup_ready).label("dedup"),
        _search_ready(now).label("search"),
    )


async def outbox_readiness(db: AsyncSession) -> dict[str, int]:
    row = (await db.execute(outbox_readiness_statement())).one()
    return {key: int(bool(getattr(row, key))) for key in _SPECS}


def _exact_ready_counts_statement(now: datetime):
    """Compatibility count used only by explicit administrator operations."""

    columns = [
        select(func.count(ImportCurationOutbox.id))
        .where(_import_ready(now))
        .scalar_subquery()
        .label("import_projection"),
        select(func.count(MediaDerivativeOutbox.id))
        .where(_leased_ready(MediaDerivativeOutbox, now))
        .scalar_subquery()
        .label("media"),
        select(func.count(AssetDedupOutbox.id))
        .where(
            or_(
                and_(
                    AssetDedupOutbox.state.in_(("pending", "failed")),
                    AssetDedupOutbox.available_at <= now,
                ),
                and_(
                    AssetDedupOutbox.state == "processing",
                    AssetDedupOutbox.updated_at <= now - timedelta(minutes=15),
                ),
            )
        )
        .scalar_subquery()
        .label("dedup"),
        select(func.count(SearchProjectionOutbox.id))
        .where(
            SearchProjectionOutbox.completed_at.is_(None),
            SearchProjectionOutbox.available_at <= now,
            or_(
                SearchProjectionOutbox.lease_until.is_(None),
                SearchProjectionOutbox.lease_until < now,
            ),
        )
        .scalar_subquery()
        .label("search"),
    ]
    if _projection_mode() == "active":
        # The compatibility endpoint only needs a useful non-zero magnitude;
        # the fallback coordinator itself always uses indexed EXISTS probes.
        columns.insert(
            2,
            select(func.count(GitlleryProjectionOutbox.id))
            .where(GitlleryProjectionOutbox.state != "complete")
            .scalar_subquery()
            .label("gitllery"),
        )
    else:
        columns.insert(2, literal(0).label("gitllery"))
    return select(*columns)


async def outbox_counts(db: AsyncSession, *, ready_only: bool = False) -> dict[str, int]:
    """Compatibility wrapper for readiness and explicit exact counts."""

    if ready_only:
        return await outbox_readiness(db)
    row = (
        await db.execute(_exact_ready_counts_statement(datetime.now(timezone.utc)))
    ).one()
    return {key: int(getattr(row, key) or 0) for key in _SPECS}


def mark_outbox_wake_pending(db: AsyncSession, kind: str) -> None:
    """Stage one Redis wake to be published only after the outer commit."""

    if kind not in _SPECS:
        raise ValueError(f"Unknown outbox kind: {kind}")
    session = getattr(db, "sync_session", db)
    info = getattr(session, "info", None)
    if not isinstance(info, dict):
        return
    get_transaction = getattr(session, "get_transaction", None)
    get_nested_transaction = getattr(session, "get_nested_transaction", None)
    transaction = (
        (get_nested_transaction() if callable(get_nested_transaction) else None)
        or (get_transaction() if callable(get_transaction) else None)
    )
    if transaction is None:
        # Lightweight test doubles have no transaction object.  Keep a single
        # outer bucket so the event helpers remain directly testable.
        transaction = "outer"
    intents = info.setdefault(_OUTBOX_WAKE_INTENTS, {})
    intents.setdefault(transaction, set()).add(kind)


@event.listens_for(Session, "before_commit")
def _arm_outbox_wakes_after_outer_commit(session: Session) -> None:
    if session.in_nested_transaction():
        return
    intents = session.info.get(_OUTBOX_WAKE_INTENTS, {})
    transaction = session.get_transaction()
    kinds = intents.get(transaction, set()) or intents.get("outer", set())
    if kinds:
        session.info[_OUTBOX_WAKE_COMMITTING] = set(kinds)


@event.listens_for(Session, "after_commit")
def _publish_outbox_wakes_after_commit(session: Session) -> None:
    intents = session.info.get(_OUTBOX_WAKE_INTENTS, {})
    nested = session.get_nested_transaction()
    if nested is not None:
        kinds = intents.pop(nested, set())
        intents.setdefault(nested.parent, set()).update(kinds)
        return
    kinds = session.info.pop(_OUTBOX_WAKE_COMMITTING, set())
    session.info.pop(_OUTBOX_WAKE_INTENTS, None)
    if not kinds:
        return
    invalidate_outbox_health_cache()
    try:
        from app.services.redis_budget import budget_redis

        # A best-effort optimization must stay below the foreground 500 ms
        # protection line.  PostgreSQL plus the scheduler fallback preserve
        # delivery when Redis is unavailable.
        with budget_redis(seconds=0.45, reserve_seconds=0):
            wake_pending_outboxes({kind: 1 for kind in sorted(kinds)})
    except Exception:
        # PostgreSQL is authoritative.  The unique scheduler fallback retries
        # after Redis or the queue becomes available again.
        logger.warning("Post-commit outbox wake failed", exc_info=True)


@event.listens_for(Session, "after_soft_rollback")
def _discard_rolled_back_outbox_wakes(session: Session, previous_transaction) -> None:
    intents = session.info.get(_OUTBOX_WAKE_INTENTS, {})
    if previous_transaction.parent is None:
        session.info.pop(_OUTBOX_WAKE_INTENTS, None)
        session.info.pop(_OUTBOX_WAKE_COMMITTING, None)
    else:
        intents.pop(previous_transaction, None)


def clear_outbox_wake(kind: str) -> None:
    """Release only the wake marker owned by the current RQ job."""

    if kind not in _SPECS:
        return
    try:
        from rq import get_current_job

        job = get_current_job()
        if job is None:
            return
        get_redis().eval(
            _CLEAR_WAKE_SCRIPT,
            1,
            f"outbox:wakeup:{kind}",
            str(job.id),
        )
    except Exception:
        # The health coordinator reconciles the marker against durable RQ
        # status, so Redis loss cannot strand the PostgreSQL outbox.
        logger.debug("Unable to clear outbox wake marker for %s", kind, exc_info=True)


def clear_and_wake_outbox_successor(
    kind: str,
    result: dict | None,
) -> dict[str, int]:
    """Atomically hand this job's token to one likely-needed successor.

    A bounded worker reports ``more_likely`` only when its item/time budget was
    exhausted. Production workers compare/swap the marker to the successor id
    before publication, closing the clear-then-health race that could bypass a
    scheduled cooldown. Exceptions pass ``None`` and deliberately rely on the
    60-second scheduler fallback rather than creating a crash loop.
    """

    if not isinstance(result, dict) or not bool(result.get("more_likely")):
        clear_outbox_wake(kind)
        return {"enqueued": 0, "deferred": 0}
    try:
        current_owner = None
        try:
            from rq import get_current_job

            current = get_current_job()
            current_owner = str(current.id) if current is not None else None
        except Exception:
            current_owner = None
        if current_owner is None:
            # Pure callers/tests without RQ context retain the compatibility
            # path. Production workers use the atomic owner handoff below.
            clear_outbox_wake(kind)
        return wake_pending_outboxes(
            {kind: 1},
            delay_seconds=max(
                0.0,
                float(result.get("successor_delay_seconds") or 0.0),
            ),
            replace_owner=current_owner,
        )
    except Exception:
        # A completed domain slice must not be reported failed merely because
        # Redis disappeared during the optimization.  PostgreSQL remains the
        # source of truth and the scheduler fallback retries within 60 seconds.
        logger.warning("Immediate outbox successor wake failed for %s", kind, exc_info=True)
        return {"enqueued": 0, "deferred": 1}


def _clear_owned_wake(redis_client, wake_key: str, job_id: str) -> None:
    """Best-effort compare/delete for a publication that did not complete."""

    try:
        redis_client.eval(_CLEAR_WAKE_SCRIPT, 1, wake_key, job_id)
    except Exception:
        # An ambiguous response keeps the marker.  Releasing it blindly could
        # delete a successor's marker and create duplicate wake jobs; the next
        # health pass reconciles it against the referenced durable RQ job.
        logger.debug("Unable to release owned outbox wake %s", wake_key, exc_info=True)


def _wake_job_is_active(redis_client, job_id: str) -> bool:
    """Return whether a marker still names executable RQ work.

    Unknown status APIs are treated as active for rolling compatibility.  A
    Redis error propagates so publication fails closed instead of creating a
    duplicate coordinator with an ambiguous queue state.
    """

    from rq.exceptions import NoSuchJobError
    from rq.job import Job
    from rq.registry import ScheduledJobRegistry

    try:
        job = Job.fetch(job_id, connection=redis_client)
    except NoSuchJobError:
        return False
    get_status = getattr(job, "get_status", None)
    if get_status is None:
        return True
    status = get_status(refresh=True)
    normalized = str(getattr(status, "value", status)).lower()
    if normalized == "scheduled":
        origin = str(getattr(job, "origin", "") or "")
        if not origin:
            return False
        registry = ScheduledJobRegistry(origin, connection=redis_client)
        if str(job_id) not in {str(item) for item in registry.get_job_ids()}:
            # RQ persists the status in the job hash separately from the
            # scheduled-registry zset.  A Redis restore or registry cleanup can
            # therefore leave a job that says ``scheduled`` but can never be
            # promoted to its queue.  Treat that split-brain record as stale so
            # the compare-delete/re-enqueue path can recover the durable outbox.
            return False
    return normalized in {"queued", "started", "deferred", "scheduled"}


async def _load_outbox_health(db: AsyncSession) -> dict[str, dict]:
    """Load exact health in the unique scheduler process."""

    models = {
        "media": MediaDerivativeOutbox,
        "dedup": AssetDedupOutbox,
    }
    if _projection_mode() == "active":
        models["gitllery"] = GitlleryProjectionOutbox
    now = datetime.now(timezone.utc)
    result: dict[str, dict] = {}
    for name, model in models.items():
        rows = await db.execute(
            select(model.state, func.count(model.id), func.min(model.created_at))
            .where(model.state.in_(_ACTIVE_STATES))
            .group_by(model.state)
        )
        counts: dict[str, int] = {}
        oldest = None
        for state, count, created_at in rows:
            counts[state] = int(count)
            if created_at and (oldest is None or created_at < oldest):
                oldest = created_at
        result[name] = {
            "waiting": counts.get("pending", 0) + counts.get("failed", 0),
            "processing": counts.get("processing", 0),
            "failed": counts.get("failed", 0),
            "oldest_age_seconds": (
                max(0.0, (now - oldest).total_seconds()) if oldest else None
            ),
        }
    if _projection_mode() == "active":
        git_target_row = (
            await db.execute(
                select(
                    func.count(GitlleryProjectionTarget.id).filter(
                        GitlleryProjectionTarget.state.in_(("pending", "failed"))
                    ),
                    func.count(GitlleryProjectionTarget.id).filter(
                        GitlleryProjectionTarget.state == "processing"
                    ),
                    func.count(GitlleryProjectionTarget.id).filter(
                        GitlleryProjectionTarget.state == "failed"
                    ),
                    func.min(GitlleryProjectionTarget.created_at).filter(
                        GitlleryProjectionTarget.state != "complete"
                    ),
                )
            )
        ).one()
        repository_modes = {
            mode: int(count)
            for mode, count in (
                await db.execute(
                    select(
                        GitlleryRepositoryState.mode,
                        func.count(GitlleryRepositoryState.id),
                    ).group_by(GitlleryRepositoryState.mode)
                )
            ).all()
        }
        target_waiting, target_processing, target_failed, target_oldest = (
            git_target_row
        )
        git_health = result["gitllery"]
        git_health.update(
            {
                "product_version": "v1",
                "format_id": "gitllery-segment",
                "format_revision": 1,
                "projection_mode": settings.gitllery_projection_mode,
                "target_waiting": int(target_waiting or 0),
                "target_processing": int(target_processing or 0),
                "target_failed": int(target_failed or 0),
                "repository_modes": repository_modes,
            }
        )
        if target_oldest:
            target_age = max(0.0, (now - target_oldest).total_seconds())
            existing_age = git_health.get("oldest_age_seconds")
            git_health["oldest_age_seconds"] = (
                target_age if existing_age is None else max(target_age, existing_age)
            )
    else:
        # Captured shadow intents are intentionally not ordinary queue work.
        # Task 4 exposes their separate build/verification status from compact
        # metadata without scanning these tables here.
        result["gitllery"] = {
            "waiting": 0,
            "processing": 0,
            "failed": 0,
            "oldest_age_seconds": None,
            "product_version": "v1",
            "format_id": "gitllery-segment",
            "format_revision": 1,
            "projection_mode": settings.gitllery_projection_mode,
            "target_waiting": 0,
            "target_processing": 0,
            "target_failed": 0,
            "repository_modes": {},
        }
    import_waiting = or_(
        ImportCurationOutbox.state.in_(("pending", "failed")),
        ImportCurationOutbox.metadata_state.in_(("pending", "failed")),
    )
    import_processing = or_(
        ImportCurationOutbox.state == "processing",
        ImportCurationOutbox.metadata_state == "processing",
    )
    import_failed = or_(
        ImportCurationOutbox.state == "failed",
        ImportCurationOutbox.metadata_state == "failed",
    )
    import_active = or_(
        ImportCurationOutbox.state.in_(_ACTIVE_STATES),
        ImportCurationOutbox.metadata_state.in_(_ACTIVE_STATES),
    )
    import_row = (
        await db.execute(
            select(
                func.count(ImportCurationOutbox.id).filter(import_waiting),
                func.count(ImportCurationOutbox.id).filter(import_processing),
                func.count(ImportCurationOutbox.id).filter(import_failed),
                func.min(ImportCurationOutbox.created_at),
                func.count(ImportCurationOutbox.id).filter(
                    ImportCurationOutbox.metadata_state.in_(("pending", "failed"))
                ),
                func.count(ImportCurationOutbox.id).filter(
                    ImportCurationOutbox.metadata_state == "processing"
                ),
                func.count(ImportCurationOutbox.id).filter(
                    ImportCurationOutbox.metadata_state == "failed"
                ),
            ).where(import_active)
        )
    ).one()
    (
        import_total_waiting,
        import_total_processing,
        import_total_failed,
        import_oldest,
        metadata_waiting,
        metadata_processing,
        metadata_failed,
    ) = import_row
    result["import_projection"] = {
        "waiting": int(import_total_waiting or 0),
        "processing": int(import_total_processing or 0),
        "failed": int(import_total_failed or 0),
        "metadata_waiting": int(metadata_waiting or 0),
        "metadata_processing": int(metadata_processing or 0),
        "metadata_failed": int(metadata_failed or 0),
        "oldest_age_seconds": (
            max(0.0, (now - import_oldest).total_seconds())
            if import_oldest
            else None
        ),
    }
    search_row = (
        await db.execute(
            select(
                func.count(SearchProjectionOutbox.id),
                func.count(SearchProjectionOutbox.id).filter(
                    SearchProjectionOutbox.lease_until.is_not(None)
                ),
                func.count(SearchProjectionOutbox.id).filter(
                    SearchProjectionOutbox.last_error.is_not(None)
                ),
                func.min(SearchProjectionOutbox.created_at),
            ).where(SearchProjectionOutbox.completed_at.is_(None))
        )
    ).one()
    search_total, search_processing, search_failed, search_oldest = search_row
    result["search"] = {
        "waiting": max(0, int(search_total or 0) - int(search_processing or 0)),
        "processing": int(search_processing or 0),
        "failed": int(search_failed or 0),
        "oldest_age_seconds": (
            max(0.0, (now - search_oldest).total_seconds())
            if search_oldest
            else None
        ),
    }
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    active_receipt = (await db.execute(
        select(SearchDeliveryReceipt).where(SearchDeliveryReceipt.state.not_in(("complete", "failed")))
    )).scalar_one_or_none()
    if active_receipt:
        result["search"]["remote_delivery"] = {
            "receipt_id": str(active_receipt.id), "state": active_receipt.state,
            "index_uid": active_receipt.index_uid, "action": active_receipt.action,
            "task_uid": active_receipt.task_uid, "available_at": active_receipt.available_at.isoformat(),
            "last_error": active_receipt.last_error,
            "recovery": "python -m app.services.search_delivery reconcile --receipt <receipt_id> --task-uid <verified_task_uid>"
                        if active_receipt.state in ("ambiguous", "submitting") else None,
        }
    return result


def invalidate_outbox_health_cache() -> None:
    global _outbox_health_cache, _outbox_health_cache_ts, _outbox_health_cache_mode
    _outbox_health_cache = None
    _outbox_health_cache_ts = 0.0
    _outbox_health_cache_mode = None


async def outbox_health(
    db: AsyncSession,
    *,
    refresh: bool = False,
) -> dict[str, dict]:
    """Return an exact snapshot cached for scheduler/background callers."""

    global _outbox_health_cache, _outbox_health_cache_ts, _outbox_health_cache_mode
    now = monotonic()
    mode = _projection_mode()
    if (
        not refresh
        and _outbox_health_cache is not None
        and _outbox_health_cache_mode == mode
        and now - _outbox_health_cache_ts < OUTBOX_HEALTH_CACHE_TTL_SECONDS
    ):
        return _outbox_health_cache
    snapshot = await _load_outbox_health(db)
    _outbox_health_cache = snapshot
    _outbox_health_cache_ts = now
    _outbox_health_cache_mode = mode
    return snapshot


async def publish_outbox_health(db: AsyncSession) -> dict[str, dict]:
    """Refresh exact counts and publish them for HTTP processes to read."""

    global _published_outbox_health
    snapshot = await outbox_health(db, refresh=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "outboxes": snapshot,
    }
    get_redis().setex(
        OUTBOX_HEALTH_REDIS_KEY,
        OUTBOX_HEALTH_REDIS_TTL_SECONDS,
        json.dumps(payload, separators=(",", ":")),
    )
    _published_outbox_health = snapshot
    return snapshot


def _empty_outbox_health() -> dict[str, dict]:
    empty = {
        "waiting": 0,
        "processing": 0,
        "failed": 0,
        "oldest_age_seconds": None,
    }
    result = {
        kind: dict(empty)
        for kind in ("import_projection", "media", "search", "dedup")
    }
    result["gitllery"] = {
        **empty,
        "projection_mode": settings.gitllery_projection_mode,
        "target_waiting": 0,
        "target_processing": 0,
        "target_failed": 0,
        "repository_modes": {},
    }
    return result


def read_published_outbox_health() -> dict[str, dict]:
    """Read scheduler-published health without ever scanning PostgreSQL."""

    global _published_outbox_health
    try:
        raw = get_redis().get(OUTBOX_HEALTH_REDIS_KEY)
        if raw:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            payload = json.loads(raw)
            snapshot = payload.get("outboxes")
            if isinstance(snapshot, dict):
                _published_outbox_health = snapshot
                return snapshot
    except Exception:
        logger.debug("Published outbox health unavailable", exc_info=True)
    return _published_outbox_health or _empty_outbox_health()


@dataclass(frozen=True)
class CompletedOutboxCleanupSpec:
    model: type
    conditions: tuple
    limit: int


def completed_outbox_cleanup_specs(
    cutoff: datetime,
    *,
    batch_size: int = 500,
) -> tuple[CompletedOutboxCleanupSpec, ...]:
    """Describe retained ordinary outboxes; Gitllery is excluded forever."""

    limit = max(1, min(int(batch_size), 500))
    return (
        CompletedOutboxCleanupSpec(
            SearchProjectionOutbox,
            (SearchProjectionOutbox.completed_at < cutoff,),
            limit,
        ),
        CompletedOutboxCleanupSpec(
            MediaDerivativeOutbox,
            (
                MediaDerivativeOutbox.state == "complete",
                MediaDerivativeOutbox.completed_at < cutoff,
            ),
            limit,
        ),
        CompletedOutboxCleanupSpec(
            AssetDedupOutbox,
            (
                AssetDedupOutbox.state == "complete",
                AssetDedupOutbox.completed_at < cutoff,
            ),
            limit,
        ),
        CompletedOutboxCleanupSpec(
            ImportCurationOutbox,
            (
                ImportCurationOutbox.state == "complete",
                ImportCurationOutbox.metadata_state == "complete",
                ImportCurationOutbox.completed_at < cutoff,
                ImportCurationOutbox.metadata_completed_at < cutoff,
            ),
            limit,
        ),
    )


async def cleanup_completed_outboxes(
    db: AsyncSession,
    *,
    retention_days: int = 30,
    batch_size: int = 500,
) -> dict[str, object]:
    """Delete at most one 500-row batch of settled, ordinary outbox history."""

    cap = max(1, min(int(batch_size), 500))
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))
    remaining = cap
    by_outbox: dict[str, int] = {}
    for spec in completed_outbox_cleanup_specs(cutoff, batch_size=cap):
        if remaining <= 0:
            break
        ids = tuple(
            (
                await db.execute(
                    select(spec.model.id)
                    .where(*spec.conditions)
                    .order_by(spec.model.completed_at, spec.model.id)
                    .limit(min(spec.limit, remaining))
                )
            ).scalars()
        )
        if not ids:
            continue
        result = await db.execute(delete(spec.model).where(spec.model.id.in_(ids)))
        deleted_count = max(0, int(result.rowcount or 0))
        by_outbox[spec.model.__tablename__] = deleted_count
        remaining -= deleted_count
    deleted = cap - remaining
    if deleted:
        await db.commit()
        invalidate_outbox_health_cache()
    return {
        "deleted": deleted,
        "by_outbox": by_outbox,
        "batch_size": cap,
        "more_likely": deleted == cap,
    }


def wake_pending_outboxes(
    counts: dict[str, int],
    *,
    delay_seconds: float = 0.0,
    replace_owner: str | None = None,
) -> dict[str, int]:
    """Publish one wake-up per health aggregation interval with real backlog.

    The coordinator is write-free while idle.  Completed jobs clear their
    marker immediately.  A marker whose RQ job is terminal or missing is
    reclaimed here, which gives crash recovery without a timeout-driven
    duplicate queue during a long resource pause.
    """
    redis = get_redis()
    queue = Queue(name="operations", connection=redis)
    enqueued = 0
    deferred = 0
    for kind, count in counts.items():
        if count <= 0:
            continue
        if kind == "gitllery" and settings.gitllery_projection_mode.strip().lower() != "active":
            continue
        wake_key = f"outbox:wakeup:{kind}"
        job_id = f"outbox-{kind}-{time.time_ns()}"
        try:
            if replace_owner is not None:
                if not redis.eval(
                    _REPLACE_WAKE_SCRIPT,
                    1,
                    wake_key,
                    replace_owner,
                    job_id,
                ):
                    continue
            else:
                existing = redis.get(wake_key)
                if isinstance(existing, bytes):
                    existing = existing.decode()
                if existing:
                    if _wake_job_is_active(redis, str(existing)):
                        continue
                    _clear_owned_wake(redis, wake_key, str(existing))
                if not redis.set(
                    wake_key,
                    job_id,
                    nx=True,
                ):
                    continue
            function, args = _SPECS[kind]
            enqueue_kwargs = {
                "job_id": job_id,
                "job_timeout": 3600,
                "result_ttl": 300,
                "failure_ttl": 300,
            }
            if delay_seconds > 0.0:
                checked_enqueue_in(
                    queue,
                    timedelta(seconds=delay_seconds),
                    function,
                    *args,
                    **enqueue_kwargs,
                )
            else:
                checked_enqueue(
                    queue,
                    function,
                    *args,
                    **enqueue_kwargs,
                )
            enqueued += 1
        except QueueAdmissionError:
            deferred += 1
            _clear_owned_wake(redis, wake_key, job_id)
        except Exception:
            deferred += 1
            _clear_owned_wake(redis, wake_key, job_id)
            logger.warning("Outbox wake-up failed for %s", kind, exc_info=True)
    return {"enqueued": enqueued, "deferred": deferred}
