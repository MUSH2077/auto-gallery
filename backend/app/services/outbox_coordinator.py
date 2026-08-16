"""Low-write RQ wake-ups for PostgreSQL-authoritative outboxes."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from rq import Queue
from sqlalchemy import and_, exists, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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


async def outbox_counts(db: AsyncSession) -> dict[str, int]:
    """Count only work that is ready to run or whose processing lease expired.

    Active rows must not generate another RQ coordinator every 15 seconds.  A
    job clears its wake marker when its bounded slice ends; if backlog remains,
    the next health tick publishes exactly one successor.
    """

    now = datetime.now(timezone.utc)
    leased_ready = lambda model: or_(  # noqa: E731 - compact scalar subqueries
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
    import_ready = or_(
        leased_ready(ImportCurationOutbox),
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
        leased_ready(GitlleryProjectionTarget),
    )
    stmt = select(
        select(func.count(ImportCurationOutbox.id))
        .where(import_ready)
        .scalar_subquery()
        .label("import_projection"),
        select(func.count(MediaDerivativeOutbox.id))
        .where(leased_ready(MediaDerivativeOutbox))
        .scalar_subquery()
        .label("media"),
        (
            select(func.count())
            .select_from(git_head)
            .where(git_head_ready)
            .scalar_subquery()
            + select(func.count(GitlleryProjectionTarget.id))
            .where(git_target_ready)
            .scalar_subquery()
        )
        .label("gitllery"),
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
    )
    row = (await db.execute(stmt)).one()
    counts = {
        key: int(getattr(row, key) or 0)
        for key in _SPECS
    }
    if settings.gitllery_projection_mode.strip().lower() != "active":
        counts["gitllery"] = 0
    return counts


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
    15-second health aggregation fallback rather than creating a crash loop.
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
        # source of truth and the health loop retries within 15 seconds.
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

    try:
        job = Job.fetch(job_id, connection=redis_client)
    except NoSuchJobError:
        return False
    get_status = getattr(job, "get_status", None)
    if get_status is None:
        return True
    status = get_status(refresh=True)
    normalized = str(getattr(status, "value", status)).lower()
    return normalized in {"queued", "started", "deferred", "scheduled"}


async def outbox_health(db: AsyncSession) -> dict[str, dict]:
    """Small background-only aggregation for the 15-second health snapshot."""
    models = {
        "media": MediaDerivativeOutbox,
        "gitllery": GitlleryProjectionOutbox,
        "dedup": AssetDedupOutbox,
    }
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
    target_waiting, target_processing, target_failed, target_oldest = git_target_row
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
    return result


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
