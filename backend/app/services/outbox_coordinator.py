"""Low-write RQ wake-ups for PostgreSQL-authoritative outboxes."""

from __future__ import annotations

import logging
import asyncio
import json
import time
from uuid import uuid4
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic

from redis.exceptions import TimeoutError as RedisTimeoutError, WatchError
from sqlalchemy.exc import SQLAlchemyError
from rq import Queue
from sqlalchemy import and_, cast, case, delete, event, exists, func, literal, or_, select, tuple_, union_all
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, aliased

from app.config import MAX_MEDIA_WORKER_CONCURRENCY, settings
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
    notify_queue_worker,
)
from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)

_CLEAR_WAKE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
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
RESOURCE_DEFERRED_KEY = "outbox:resource-deferred"
_CLEAR_DEFERRED_SCRIPT = """
if redis.call('hget', KEYS[1], ARGV[1]) == ARGV[2] then
  return redis.call('hdel', KEYS[1], ARGV[1])
end
return 0
"""
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
_RESOURCE_DESCRIPTORS = {
    "import_projection": "import-projection",
    "media": "media-derivatives",
    "gitllery": "gitllery-projection",
    "search": "search-projection",
    "dedup": "asset-dedup-outbox",
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
    from app.services.search_index_definitions import WORKS_INDEX

    search_curation_ready = or_(
        SearchProjectionOutbox.index_uid != WORKS_INDEX,
        SearchProjectionOutbox.action == "delete",
        ~exists().where(and_(
            ImportCurationOutbox.work_id == cast(
                SearchProjectionOutbox.entity_id, PG_UUID(as_uuid=True)
            ),
            ImportCurationOutbox.state != "complete",
        )),
    )
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
                search_curation_ready,
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


def clear_outbox_wake(kind: str) -> bool:
    """Release only the wake marker owned by the current RQ job."""

    if kind not in _SPECS:
        return False
    try:
        from rq import get_current_job

        job = get_current_job()
        if job is None:
            return False
        return bool(get_redis().eval(
            _CLEAR_WAKE_SCRIPT,
            1,
            _wake_key(kind, _job_wake_slot(job)),
            str(job.id),
        ))
    except Exception:
        # The health coordinator reconciles the marker against durable RQ
        # status, so Redis loss cannot strand the PostgreSQL outbox.
        logger.debug("Unable to clear outbox wake marker for %s", kind, exc_info=True)
        return False


def clear_and_wake_outbox_successor(
    kind: str,
    result: dict | None,
) -> dict[str, int]:
    """Atomically hand this job's token to one likely-needed successor.

    A bounded worker reports ``more_likely`` only when its item/time budget was
    exhausted. Production workers replace the marker and save the successor RQ
    record in one transaction, preserving ownership and any scheduled cooldown.
    Exceptions pass ``None`` and deliberately rely on the
    60-second scheduler fallback rather than creating a crash loop.
    """

    if isinstance(result, dict) and result.get("resource_deferred"):
        try:
            # A new token per deferral prevents an older completion from
            # clearing a concurrent runner's resource-release notification.
            get_redis().hset(RESOURCE_DEFERRED_KEY, kind, uuid4().hex)
        except Exception:
            logger.debug("Unable to record deferred outbox wake", exc_info=True)
    if not isinstance(result, dict) or not bool(result.get("more_likely")):
        released = clear_outbox_wake(kind)
        if released and isinstance(result, dict) and not result.get("resource_deferred"):
            # Clear first: commits before this point are visible to the read;
            # later commits can publish against the now-empty owner key.
            return _wake_after_empty_slice(kind)
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


async def _deferred_outbox_readiness() -> dict[str, int]:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool

    # The RQ parent forks subsequent jobs and uses new event loops. Never
    # leave pooled async database connections in that parent process.
    engine = create_async_engine(
        settings.database_url, poolclass=NullPool, connect_args=_recovery_sql_options(),
    )
    try:
        async with AsyncSession(engine) as db:
            return await outbox_counts(db, ready_only=True)
    finally:
        await engine.dispose()


# Far-future cleanup keeps the normal timer fallback instead of retaining an
# RQ job for days. This is a scheduling horizon, never an early-claim allowance.
_EMPTY_WAKE_HORIZON_SECONDS = 60.0


def _next_outbox_deadline_statement(kind: str, now: datetime):
    def leased(state, available, lease):
        return select(func.min(case(
            (state.in_(("pending", "failed")), available),
            (state == "processing", lease),
        )).label("due"))

    if kind == "media":
        model = MediaDerivativeOutbox
        queries = [leased(model.state, model.available_at, model.lease_expires_at)]
    elif kind == "import_projection":
        model = ImportCurationOutbox
        queries = [leased(model.state, model.available_at, model.lease_expires_at),
            leased(model.metadata_state, model.metadata_available_at, model.metadata_lease_expires_at)]
    elif kind == "dedup":
        model = AssetDedupOutbox
        queries = [leased(model.state, model.available_at, model.updated_at + timedelta(minutes=15))]
    elif kind == "search":
        from app.models.repository_sync_receipt import SearchIndexState
        from app.models.search_delivery_receipt import SearchDeliveryReceipt
        from app.models.search_rebuild import SearchRebuild
        from app.services.search_rebuild import membership_bootstrap_due_condition
        from app.services.search_index_definitions import WORKS_INDEX

        search_curation_ready = or_(
            SearchProjectionOutbox.index_uid != WORKS_INDEX,
            SearchProjectionOutbox.action == "delete",
            ~exists().where(and_(
                ImportCurationOutbox.work_id == cast(
                    SearchProjectionOutbox.entity_id, PG_UUID(as_uuid=True)
                ),
                ImportCurationOutbox.state != "complete",
            )),
        )
        receipt = SearchDeliveryReceipt
        active_delivery = exists().where(receipt.state.not_in(("complete", "failed")))
        queries = [
            select(func.min(func.greatest(receipt.available_at, receipt.lease_until)).label("due"))
                .where(receipt.state.not_in(("complete", "failed", "ambiguous"))),
            select(func.min(func.greatest(SearchProjectionOutbox.available_at, SearchProjectionOutbox.lease_until)).label("due"))
                .where(~active_delivery, SearchProjectionOutbox.completed_at.is_(None), search_curation_ready),
            select(func.min(func.coalesce(SearchIndexState.last_verified_at + timedelta(seconds=30), now)).label("due"))
                .where(~active_delivery, SearchIndexState.database_generation > SearchIndexState.indexed_generation,
                    ~exists().where(SearchProjectionOutbox.index_uid == SearchIndexState.index_uid,
                        SearchProjectionOutbox.completed_at.is_(None))),
            select(literal(now).label("due")).where(exists().where(SearchRebuild.state.not_in(("complete", "failed")))),
            select(literal(now).label("due")).where(~active_delivery, membership_bootstrap_due_condition()),
        ]
    else:
        return None
    deadlines = union_all(*queries).subquery()
    return select(func.min(deadlines.c.due)).where(
        deadlines.c.due <= now + timedelta(seconds=_EMPTY_WAKE_HORIZON_SECONDS)
    )


async def _deferred_outbox_next_delays(kinds: set[str]) -> dict[str, float]:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(
        settings.database_url, poolclass=NullPool, connect_args=_recovery_sql_options(),
    )
    try:
        async with AsyncSession(engine) as db:
            delays = {}
            for kind in sorted(kinds):
                statement = _next_outbox_deadline_statement(kind, datetime.now(timezone.utc))
                if statement is None:
                    continue
                due = await db.scalar(statement)
                if due is not None:
                    # RQ scheduled scores have one-second precision. A short
                    # delayed recheck avoids spinning on a still-locked row.
                    delays[kind] = max(1.0, (due - datetime.now(timezone.utc)).total_seconds())
            return delays
    finally:
        await engine.dispose()


def _wake_after_empty_slice(kind: str) -> dict[str, int]:
    from app.services.redis_budget import budget_redis

    try:
        with budget_redis(seconds=0.5, reserve_seconds=0):
            delays = asyncio.run(asyncio.wait_for(_deferred_outbox_next_delays({kind}), timeout=0.5))
            if kind in delays:
                return wake_pending_outboxes({kind: 1}, delay_seconds=delays[kind])
    except Exception:
        # The slice is complete; an unavailable recheck must not fail it or
        # invent ready work. PostgreSQL and the existing timer retain recovery.
        logger.debug("Empty outbox follow-up check unavailable for %s", kind, exc_info=True)
        return {"enqueued": 0, "deferred": 1}
    return {"enqueued": 0, "deferred": 0}


async def _admissible_deferred_outboxes(kinds: set[str], redis_client) -> set[str]:
    """Check current admission without claiming work or opening parent pools."""
    from app.services.heavy_io import (
        collect_active_resource_leases, background_turn_available,
        local_lock_for_workload, resource_reservation_details,
    )
    from app.services.resource_descriptors import descriptor_by_name
    from app.services.resource_pressure import profile_slice_limits

    snapshot = _recovery_pressure_snapshot(redis_client)
    if snapshot is None:
        return set()
    leases = collect_active_resource_leases(redis_client=redis_client)
    if leases.get("error"):
        return set()
    reserved = int(leases.get("reserved_bytes") or 0)
    media_profiles = {"image_derive", "video_derive"}
    if "media" in kinds:
        from app.models.asset import Asset
        from app.services.media_derivatives import _video_candidate_condition
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        engine = create_async_engine(
            settings.database_url, poolclass=NullPool, connect_args=_recovery_sql_options(),
        )
        try:
            async with AsyncSession(engine) as db:
                video = _video_candidate_condition()
                condition = _leased_ready(MediaDerivativeOutbox, datetime.now(timezone.utc))
                row = (await db.execute(select(
                    exists().where(MediaDerivativeOutbox.asset_id == Asset.id, condition, ~video).label("image"),
                    exists().where(MediaDerivativeOutbox.asset_id == Asset.id, condition, video).label("video"),
                ))).one()
                media_profiles = {profile for profile, ready in (
                    ("image_derive", row.image), ("video_derive", row.video),
                ) if ready}
        finally:
            await engine.dispose()
    admitted = set()
    for kind in kinds:
        descriptor = descriptor_by_name(_RESOURCE_DESCRIPTORS[kind])
        profiles = media_profiles if kind == "media" else {descriptor.profile}
        for profile in profiles:
            if not background_turn_available(redis_client):
                continue
            if not profile_slice_limits(snapshot, profile).allowed:
                continue
            if kind == "media":
                from app.services.media_admission import media_profile_admissible

                if not media_profile_admissible(snapshot, profile, leases):
                    continue
            else:
                requested, capacity = resource_reservation_details(snapshot, profile)
                if reserved + requested > capacity:
                    continue
            if kind != "media":
                lock = local_lock_for_workload(profile, lane="background" if kind == "import_projection" else None)
                if lock is not None:
                    try:
                        if not lock.try_acquire():
                            continue
                    finally:
                        lock.release()
            admitted.add(kind)
            break
    return admitted


def _recovery_sql_options() -> dict[str, float]:
    from app.services.redis_budget import current_work_deadline

    remaining = max(0.001, current_work_deadline() - monotonic())
    return {"timeout": remaining, "command_timeout": remaining}


def _recovery_pressure_snapshot(redis_client) -> dict | None:
    from app.services.resource_pressure_snapshot import read_shared_resource_pressure_snapshot
    from app.services.resource_pressure import read_resource_pressure_latch

    snapshot = read_shared_resource_pressure_snapshot(redis_client=redis_client)
    if snapshot is None or read_resource_pressure_latch(redis_client=redis_client):
        # Recovery is a hint, not a sampling path. A missing snapshot or live
        # pause latch must wait for the pressure monitor; never sample in the
        # RQ parent or leave an executor thread alive before its next fork.
        return None
    return snapshot


async def _inspect_deferred_outboxes(kinds: set[str], redis_client):
    counts = await _deferred_outbox_readiness()
    ready = {kind for kind in kinds if counts.get(kind)}
    admitted = await _admissible_deferred_outboxes(ready, redis_client) if ready else set()
    future = await _deferred_outbox_next_delays(kinds - ready) if kinds - ready else {}
    return counts, ready, admitted, future


def wake_resource_deferred_outboxes(*, timeout_seconds: float = 0.5) -> dict[str, int]:
    """Bound recovery reads and publication; retain durable work on timeout."""
    from app.services.redis_budget import budget_redis
    from app.services.remote_search_flight import read_marker
    from app.services.heavy_io import background_turn_available

    deadline = monotonic() + max(0.01, timeout_seconds)
    try:
        with budget_redis(seconds=max(0.01, deadline - monotonic()), reserve_seconds=0):
            redis = get_redis()
            deferred = redis.hgetall(RESOURCE_DEFERRED_KEY)
            if not deferred or not background_turn_available(redis):
                return {"enqueued": 0, "deferred": 0}
            tokens = {(k.decode() if isinstance(k, bytes) else k): v for k, v in deferred.items()}
            kinds = set(tokens) & set(_SPECS)
            if not kinds or redis.exists("lock:resource-budget:background", "lock:heavy-io") or read_marker():
                return {"enqueued": 0, "deferred": 0}
            remaining = deadline - monotonic()
            if remaining <= 0:
                return {"enqueued": 0, "deferred": 1}
            counts, ready, admitted, future = asyncio.run(asyncio.wait_for(
                _inspect_deferred_outboxes(kinds, redis), timeout=remaining,
            ))
        remaining = deadline - monotonic()
        if remaining <= 0:
            return {"enqueued": 0, "deferred": 1}
        # Recreate short sockets for the remaining publication budget.
        with budget_redis(seconds=remaining, reserve_seconds=0):
            redis = get_redis()
            outcome = {"enqueued": 0, "deferred": 0}
            for kind in kinds - ready:
                if kind in future:
                    wake = wake_pending_outboxes({kind: 1}, delay_seconds=future[kind])
                    outcome["enqueued"] += wake["enqueued"]
                    outcome["deferred"] += wake["deferred"] or int(not wake["enqueued"])
                    if not wake["enqueued"]:
                        continue  # A current runner may not have cleared yet.
                redis.eval(_CLEAR_DEFERRED_SCRIPT, 1, RESOURCE_DEFERRED_KEY, kind, tokens[kind])
            if admitted:
                wake = wake_pending_outboxes({kind: counts[kind] for kind in ready.intersection(admitted)})
                for key in outcome:
                    outcome[key] += wake[key]
            return outcome
    except (TimeoutError, RedisTimeoutError, OSError, SQLAlchemyError):
        logger.debug("Resource recovery check timed out; retaining deferred work", exc_info=True)
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


def _wake_key(kind: str, slot: int = 0) -> str:
    return f"outbox:wakeup:{kind}" + (f":{slot}" if slot else "")


def _job_wake_slot(job) -> int:
    return int((getattr(job, "meta", None) or {}).get("outbox_wake_slot", 0))


def _retire_media_wakes(redis_client, limit: int) -> None:
    """Remove terminal retired slots without detaching still-running jobs."""
    keys = [_wake_key("media", slot) for slot in range(limit, MAX_MEDIA_WORKER_CONCURRENCY)]
    if not keys:
        return
    for key, owner in zip(keys, redis_client.mget(keys)):
        if not owner:
            continue
        if isinstance(owner, bytes):
            owner = owner.decode()
        if not _wake_job_is_active(redis_client, str(owner)):
            _clear_owned_wake(redis_client, key, str(owner))


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
    if remaining > 0:
        from app.services.postgres_search_store import cleanup_tasks
        cleaned = await cleanup_tasks(db, cutoff, limit=remaining)
        if cleaned:
            by_outbox["search_pg_tasks"] = cleaned
        remaining -= cleaned
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


class _AtomicOutboxQueue(Queue):
    """Keep delayed registry writes inside the caller's transaction as well."""

    def schedule_job(self, job, datetime, pipeline=None, unique=False):
        if pipeline is None or unique:
            return super().schedule_job(job, datetime, pipeline=pipeline, unique=unique)
        from calendar import timegm
        from rq.job import JobStatus
        from rq.registry import ScheduledJobRegistry

        # RQ's registry.schedule currently writes ZADD on its connection even
        # when given a pipeline. Use its normal persistence with an explicit
        # pipelined registry write, so a failed WATCH leaves no orphan entry.
        registry = ScheduledJobRegistry(queue=self)
        pipeline.sadd(self.redis_queues_keys, self.key)
        self._persist_job(job, pipeline, status=JobStatus.SCHEDULED)
        pipeline.zadd(registry.key, {job.id: timegm(datetime.utctimetuple())})
        return job


def wake_pending_outboxes(
    counts: dict[str, int],
    *,
    delay_seconds: float = 0.0,
    replace_owner: str | None = None,
) -> dict[str, int]:
    """Publish bounded media wakes and one wake for every other outbox kind.

    The coordinator is write-free while idle.  Completed jobs clear their
    marker immediately.  A marker whose RQ job is terminal or missing is
    reclaimed here, which gives crash recovery without a timeout-driven
    duplicate queue during a long resource pause.
    """
    redis = get_redis()
    queue = _AtomicOutboxQueue(name="operations", connection=redis)
    enqueued = 0
    deferred = 0
    wake_slots = []
    for kind, count in counts.items():
        if count <= 0:
            continue
        if kind == "gitllery" and settings.gitllery_projection_mode.strip().lower() != "active":
            continue
        limit = settings.media_worker_concurrency if kind == "media" else 1
        if kind == "media":
            _retire_media_wakes(redis, limit)
        if replace_owner is not None:
            from rq import get_current_job

            slot = _job_wake_slot(get_current_job())
            if slot >= limit:
                _clear_owned_wake(redis, _wake_key(kind, slot), replace_owner)
                continue
            slots = (slot,)
        else:
            # Post-commit counts are readiness hints, not an exact asset count.
            # Fill the configured pool; empty runners only retain near-deadline work.
            slots = range(limit)
        wake_slots.extend((kind, slot) for slot in slots)
    for kind, slot in wake_slots:
        wake_key = _wake_key(kind, slot)
        job_id = f"outbox-{kind}-{slot}-{time.time_ns()}"
        function, args = _SPECS[kind]
        enqueue_kwargs = {
            "job_id": job_id,
            "job_timeout": 3600,
            "result_ttl": 300,
            "failure_ttl": 300,
            "meta": {"resource_descriptor": _RESOURCE_DESCRIPTORS[kind]},
        }
        if kind == "media":
            enqueue_kwargs["meta"]["outbox_wake_slot"] = slot
        try:
            if _publish_outbox_wake(
                redis, queue, wake_key, function, args, enqueue_kwargs,
                delay_seconds=delay_seconds, replace_owner=replace_owner,
            ):
                enqueued += 1
        except QueueAdmissionError:
            deferred += 1
        except Exception:
            deferred += 1
            # EXEC may have committed despite a lost response. Keep its owner;
            # durable RQ status and the health fallback reconcile ambiguity.
            logger.warning("Outbox wake-up failed for %s", kind, exc_info=True)
    return {"enqueued": enqueued, "deferred": deferred}


def _publish_outbox_wake(
    redis, queue, wake_key: str, function: str, args: tuple, enqueue_kwargs: dict,
    *, delay_seconds: float, replace_owner: str | None,
) -> bool:
    """Commit the owner and executable RQ record in the same Redis transaction.

    Publishing an owner before saving its job lets another writer mistake an
    in-flight publication for a crashed job. WATCH also preserves successor
    ownership without introducing a publication TTL or expiring queued work.
    """
    for _ in range(3):
        try:
            with redis.pipeline() as pipeline:
                pipeline.watch(wake_key)
                owner = pipeline.get(wake_key)
                if isinstance(owner, bytes):
                    owner = owner.decode()
                if replace_owner is not None:
                    if owner != replace_owner:
                        return False
                elif owner and _wake_job_is_active(redis, str(owner)):
                    return False
                pipeline.multi()
                pipeline.set(wake_key, enqueue_kwargs["job_id"])
                if delay_seconds > 0:
                    checked_enqueue_in(
                        queue, timedelta(seconds=delay_seconds), function, *args,
                        pipeline=pipeline, **enqueue_kwargs,
                    )
                else:
                    checked_enqueue(
                        queue, function, *args, pipeline=pipeline, **enqueue_kwargs,
                    )
                pipeline.execute()
            if delay_seconds <= 0:
                notify_queue_worker(queue.name, redis)
            return True
        except WatchError:
            # A concurrent producer or finishing worker changed the owner.
            # Re-read its durable status rather than stealing the new marker.
            continue
    raise WatchError("Outbox wake owner kept changing during publication")
