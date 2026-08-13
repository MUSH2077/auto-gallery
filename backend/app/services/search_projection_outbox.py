"""Bounded, durable delivery of database state into search projections.

This module owns only the transactional queue mechanics.  Document assembly and
Meilisearch I/O stay in :mod:`app.services.search`, which lets the queue be
claimed without importing the comparatively heavy search SDK in every process.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Iterable
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, event, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.config import settings
from app.database import async_session
from app.models.search_projection_outbox import SearchProjectionOutbox
from app.models.repository_sync_receipt import SearchIndexState
from app.services.cache import cache_bump_generation


OUTBOX_SQL_BATCH_SIZE = 500
DEFAULT_WORKS_INDEX_UID = f"{settings.meili_index_prefix}works"
DEFAULT_CREATORS_INDEX_UID = f"{settings.meili_index_prefix}creators"
DEFAULT_TAGS_INDEX_UID = f"{settings.meili_index_prefix}tags"
DEFAULT_REPOSITORIES_INDEX_UID = f"{settings.meili_index_prefix}repositories"
DEFAULT_SUBSCRIPTIONS_INDEX_UID = f"{settings.meili_index_prefix}subscriptions"

logger = logging.getLogger(__name__)

_WORKS_GENERATION_PENDING = "search_works_generation_pending"
_WORKS_GENERATION_COMMITTING = "search_works_generation_committing"


@event.listens_for(Session, "before_commit")
def _arm_works_generation_after_outer_commit(session: Session) -> None:
    """Distinguish an outer commit from a SAVEPOINT release."""

    if session.info.get(_WORKS_GENERATION_PENDING) and not session.in_nested_transaction():
        session.info[_WORKS_GENERATION_COMMITTING] = True


@event.listens_for(Session, "after_commit")
def _bump_works_generation_after_commit(session: Session) -> None:
    """Invalidate exact totals only after their domain mutation is visible."""

    if not session.info.pop(_WORKS_GENERATION_COMMITTING, False):
        return
    session.info.pop(_WORKS_GENERATION_PENDING, None)
    cache_bump_generation("works")


def mark_works_generation_pending(db: AsyncSession) -> None:
    """Request one works-generation bump after this outer transaction commits."""

    db.info[_WORKS_GENERATION_PENDING] = True


@dataclass(frozen=True)
class ProjectionEvent:
    id: UUID
    index_uid: str
    entity_id: str
    action: str
    version: int
    attempts: int
    updated_at: datetime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _event(row: SearchProjectionOutbox) -> ProjectionEvent:
    return ProjectionEvent(
        id=row.id,
        index_uid=row.index_uid,
        entity_id=row.entity_id,
        action=row.action,
        version=int(row.version),
        attempts=int(row.attempts),
        updated_at=row.updated_at,
    )


async def _enqueue_projection_action(
    db: AsyncSession,
    index_uid: str,
    identities: tuple[str, ...],
    *,
    action: str,
) -> int:
    if not identities:
        return 0
    for start in range(0, len(identities), OUTBOX_SQL_BATCH_SIZE):
        rows = [
            {
                "index_uid": index_uid,
                "entity_id": entity_id,
                "action": action,
                "version": 1,
                "attempts": 0,
            }
            for entity_id in identities[start:start + OUTBOX_SQL_BATCH_SIZE]
        ]
        insert_stmt = pg_insert(SearchProjectionOutbox).values(rows)
        statement = insert_stmt.on_conflict_do_update(
            index_elements=["index_uid", "entity_id"],
            set_={
                "action": insert_stmt.excluded.action,
                "version": SearchProjectionOutbox.version + 1,
                "attempts": 0,
                "available_at": func.now(),
                "lease_until": None,
                "completed_at": None,
                "last_error": None,
                "updated_at": func.now(),
            },
        )
        await db.execute(statement)
    return len(identities)


async def _mark_index_changed(db: AsyncSession, index_uid: str) -> None:
    insert_state = pg_insert(SearchIndexState).values(
        id=uuid4(),
        index_uid=index_uid,
        database_generation=1,
        indexed_generation=0,
        status="catching_up",
    )
    await db.execute(
        insert_state.on_conflict_do_update(
            index_elements=["index_uid"],
            set_={
                "database_generation": SearchIndexState.database_generation + 1,
                "status": "catching_up",
                "updated_at": func.now(),
            },
        )
    )


async def request_search_projection(
    db: AsyncSession,
    work_ids: Iterable[str | UUID] = (),
    *,
    deleted_work_ids: Iterable[str | UUID] = (),
    creator_ids: Iterable[str | UUID] = (),
    deleted_creator_ids: Iterable[str | UUID] = (),
    tag_ids: Iterable[str | UUID] = (),
    deleted_tag_ids: Iterable[str | UUID] = (),
    repository_ids: Iterable[str | UUID] = (),
    deleted_repository_ids: Iterable[str | UUID] = (),
    subscription_ids: Iterable[str | UUID] = (),
    deleted_subscription_ids: Iterable[str | UUID] = (),
    index_uid: str | None = None,
) -> int:
    """Request work/reference projection changes in the caller's transaction.

    Import and mutation code should call this before committing the database
    changes it describes.  A rollback then removes both the domain mutation and
    its search request; a commit makes both durable together.  IDs are chunked
    well below asyncpg's 32,767 bind-parameter ceiling.  If an ID appears in
    both collections, the delete is applied last and therefore wins.
    """

    requests = (
        (index_uid or DEFAULT_WORKS_INDEX_UID, work_ids, deleted_work_ids),
        (DEFAULT_CREATORS_INDEX_UID, creator_ids, deleted_creator_ids),
        (DEFAULT_TAGS_INDEX_UID, tag_ids, deleted_tag_ids),
        (DEFAULT_REPOSITORIES_INDEX_UID, repository_ids, deleted_repository_ids),
        (DEFAULT_SUBSCRIPTIONS_INDEX_UID, subscription_ids, deleted_subscription_ids),
    )
    requested = 0
    work_requested = False
    changed_indexes: set[str] = set()
    for position, (projection_uid, upsert_values, delete_values) in enumerate(requests):
        upserts = tuple(dict.fromkeys(str(value) for value in upsert_values))
        deletes = tuple(dict.fromkeys(str(value) for value in delete_values))
        if upserts:
            requested += await _enqueue_projection_action(
                db,
                projection_uid,
                upserts,
                action="upsert",
            )
            changed_indexes.add(projection_uid)
        if deletes:
            requested += await _enqueue_projection_action(
                db,
                projection_uid,
                deletes,
                action="delete",
            )
            changed_indexes.add(projection_uid)
        if position == 0 and (upserts or deletes):
            work_requested = True
    if work_requested:
        # The synchronous Session event behind AsyncSession bumps Redis only
        # after the *outer* transaction commits.  Bumping here would let a
        # concurrent reader populate the new generation from pre-commit rows.
        mark_works_generation_pending(db)
    for projection_uid in changed_indexes:
        await _mark_index_changed(db, projection_uid)
    return requested


async def enqueue_projection_events(
    index_uid: str,
    entity_ids: Iterable[str | UUID],
    *,
    action: str = "upsert",
) -> int:
    """Compatibility helper that persists requests in its own transaction.

    New mutation paths should prefer :func:`request_search_projection` with
    their existing session.  This wrapper remains useful when a caller is
    already post-commit or when delivering a deletion notification.
    """

    if action not in {"upsert", "delete"}:
        raise ValueError(f"Unsupported search projection action: {action}")
    identities = tuple(dict.fromkeys(str(value) for value in entity_ids))
    async with async_session() as db:
        requested = await _enqueue_projection_action(
            db,
            index_uid,
            identities,
            action=action,
        )
        if requested:
            await _mark_index_changed(db, index_uid)
        if requested and index_uid == DEFAULT_WORKS_INDEX_UID:
            db.info[_WORKS_GENERATION_PENDING] = True
        await db.commit()
    return requested


async def claim_projection_events(
    *,
    limit: int = 500,
    lease_seconds: int = 180,
) -> list[ProjectionEvent]:
    """Lease ready events with ``SKIP LOCKED`` so drains never duplicate work."""

    now = _utcnow()
    async with async_session() as db:
        rows = list((await db.execute(
            select(SearchProjectionOutbox)
            .where(
                SearchProjectionOutbox.completed_at.is_(None),
                SearchProjectionOutbox.available_at <= now,
                or_(
                    SearchProjectionOutbox.lease_until.is_(None),
                    SearchProjectionOutbox.lease_until <= now,
                ),
            )
            .order_by(
                SearchProjectionOutbox.available_at,
                SearchProjectionOutbox.updated_at,
                SearchProjectionOutbox.id,
            )
            .limit(max(1, min(int(limit), 1000)))
            .with_for_update(skip_locked=True)
        )).scalars().all())
        lease_until = now + timedelta(seconds=max(30, int(lease_seconds)))
        if rows:
            await db.execute(
                update(SearchProjectionOutbox)
                .where(SearchProjectionOutbox.id.in_([row.id for row in rows]))
                .values(
                    lease_until=lease_until,
                    # Leasing is delivery bookkeeping, not a new projection
                    # mutation.  Preserve the replay/keyset timestamp.
                    updated_at=SearchProjectionOutbox.updated_at,
                )
            )
        await db.commit()
        return [_event(row) for row in rows]


def _matching_events(events: Iterable[ProjectionEvent]):
    pairs = tuple((event.id, event.version) for event in events)
    if not pairs:
        return None
    return or_(*(
        and_(
            SearchProjectionOutbox.id == event_id,
            SearchProjectionOutbox.version == version,
        )
        for event_id, version in pairs
    ))


async def complete_projection_events(events: Iterable[ProjectionEvent]) -> int:
    """Acknowledge only the exact versions that were delivered."""

    delivered = tuple(events)
    if not delivered:
        return 0
    changed = 0
    async with async_session() as db:
        for start in range(0, len(delivered), OUTBOX_SQL_BATCH_SIZE):
            condition = _matching_events(
                delivered[start:start + OUTBOX_SQL_BATCH_SIZE]
            )
            result = await db.execute(
                update(SearchProjectionOutbox)
                .where(condition)
                .values(
                    completed_at=func.now(),
                    lease_until=None,
                    last_error=None,
                    updated_at=func.now(),
                )
            )
            changed += int(result.rowcount or 0)
        await db.commit()
    return changed


async def complete_projection_versions(
    versions: Iterable[tuple[UUID, int]],
) -> int:
    """Acknowledge compact rebuild records without retaining rich events."""

    delivered = tuple(versions)
    if not delivered:
        return 0
    changed = 0
    async with async_session() as db:
        for start in range(0, len(delivered), OUTBOX_SQL_BATCH_SIZE):
            pairs = delivered[start:start + OUTBOX_SQL_BATCH_SIZE]
            condition = or_(*(
                and_(
                    SearchProjectionOutbox.id == event_id,
                    SearchProjectionOutbox.version == version,
                )
                for event_id, version in pairs
            ))
            result = await db.execute(
                update(SearchProjectionOutbox)
                .where(condition)
                .values(
                    completed_at=func.now(),
                    lease_until=None,
                    last_error=None,
                    updated_at=func.now(),
                )
            )
            changed += int(result.rowcount or 0)
        await db.commit()
    return changed


async def release_projection_events(events: Iterable[ProjectionEvent]) -> int:
    """Release unselected claims without counting them as failed attempts."""

    released = tuple(events)
    if not released:
        return 0
    changed = 0
    async with async_session() as db:
        for start in range(0, len(released), OUTBOX_SQL_BATCH_SIZE):
            condition = _matching_events(
                released[start:start + OUTBOX_SQL_BATCH_SIZE]
            )
            result = await db.execute(
                update(SearchProjectionOutbox)
                .where(condition)
                .values(
                    lease_until=None,
                    updated_at=SearchProjectionOutbox.updated_at,
                )
            )
            changed += int(result.rowcount or 0)
        await db.commit()
    return changed


async def retry_projection_events(
    events: Iterable[ProjectionEvent],
    error: str,
) -> int:
    """Release failed claims with bounded exponential backoff."""

    claimed = tuple(events)
    if not claimed:
        return 0
    highest_attempt = max((event.attempts for event in claimed), default=0) + 1
    delay = min(900, 5 * (2 ** min(highest_attempt - 1, 7)))
    changed = 0
    async with async_session() as db:
        for start in range(0, len(claimed), OUTBOX_SQL_BATCH_SIZE):
            condition = _matching_events(
                claimed[start:start + OUTBOX_SQL_BATCH_SIZE]
            )
            result = await db.execute(
                update(SearchProjectionOutbox)
                .where(condition)
                .values(
                    attempts=SearchProjectionOutbox.attempts + 1,
                    available_at=_utcnow() + timedelta(seconds=delay),
                    lease_until=None,
                    completed_at=None,
                    last_error=(error or "search projection delivery failed")[:4000],
                    updated_at=func.now(),
                )
            )
            changed += int(result.rowcount or 0)
        await db.commit()
    return changed


async def replay_projection_events(
    index_uid: str,
    *,
    since: datetime,
    after: tuple[datetime, UUID] | None = None,
    limit: int = 1000,
) -> list[ProjectionEvent]:
    """Return changes a staging rebuild must replay before its atomic swap.

    Pending rows from before ``since`` are included as well: their authoritative
    state may never have reached the old live index.
    """

    conditions = [
        SearchProjectionOutbox.index_uid == index_uid,
        or_(
            SearchProjectionOutbox.completed_at.is_(None),
            SearchProjectionOutbox.updated_at >= since,
        ),
    ]
    if after is not None:
        conditions.append(
            tuple_(
                SearchProjectionOutbox.updated_at,
                SearchProjectionOutbox.id,
            ) > tuple_(after[0], after[1])
        )
    async with async_session() as db:
        rows = (await db.execute(
            select(SearchProjectionOutbox)
            .where(*conditions)
            .order_by(SearchProjectionOutbox.updated_at, SearchProjectionOutbox.id)
            .limit(max(1, min(int(limit), 1000)))
        )).scalars().all()
        return [_event(row) for row in rows]


async def prune_completed_projection_events(*, retention_hours: int = 24) -> int:
    """Remove replay history only after it is safely outside the rebuild window."""

    cutoff = _utcnow() - timedelta(hours=max(1, int(retention_hours)))
    async with async_session() as db:
        result = await db.execute(
            delete(SearchProjectionOutbox).where(
                SearchProjectionOutbox.completed_at.is_not(None),
                SearchProjectionOutbox.completed_at < cutoff,
            )
        )
        await db.commit()
        return int(result.rowcount or 0)


async def search_projection_outbox_health() -> dict[str, object]:
    """Return a cheap aggregate suitable for the system health endpoint."""

    now = _utcnow()
    try:
        async with async_session() as db:
            row = (await db.execute(
                select(
                    func.count().filter(
                        SearchProjectionOutbox.completed_at.is_(None)
                    ),
                    func.count().filter(
                        SearchProjectionOutbox.completed_at.is_(None),
                        SearchProjectionOutbox.available_at <= now,
                        or_(
                            SearchProjectionOutbox.lease_until.is_(None),
                            SearchProjectionOutbox.lease_until <= now,
                        ),
                    ),
                    func.count().filter(
                        SearchProjectionOutbox.completed_at.is_(None),
                        SearchProjectionOutbox.lease_until > now,
                    ),
                    func.count().filter(
                        SearchProjectionOutbox.completed_at.is_(None),
                        SearchProjectionOutbox.attempts > 0,
                    ),
                    func.min(SearchProjectionOutbox.updated_at).filter(
                        SearchProjectionOutbox.completed_at.is_(None)
                    ),
                    func.max(SearchProjectionOutbox.attempts).filter(
                        SearchProjectionOutbox.completed_at.is_(None)
                    ),
                )
            )).one()
        pending, ready, leased, retrying, oldest, max_attempts = row
        oldest_age_seconds = None
        if oldest is not None:
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=timezone.utc)
            oldest_age_seconds = max(0, int((now - oldest).total_seconds()))
        status = "degraded" if int(retrying or 0) else "ok"
        return {
            "status": status,
            "pending": int(pending or 0),
            "ready": int(ready or 0),
            "leased": int(leased or 0),
            "retrying": int(retrying or 0),
            "max_attempts": int(max_attempts or 0),
            "oldest_age_seconds": oldest_age_seconds,
        }
    except Exception as exc:
        logger.warning("Search projection outbox health query failed", exc_info=True)
        return {
            "status": "unavailable",
            "pending": None,
            "ready": None,
            "leased": None,
            "retrying": None,
            "max_attempts": None,
            "oldest_age_seconds": None,
            "error": str(exc),
        }


async def drain_search_projection_outbox_batches(
    *,
    batch_size: int = 500,
    max_batches: int = 20,
) -> dict[str, int | str]:
    """Drain bounded batches; this is the async seam for an RQ operation."""

    # Delayed import avoids a module cycle: search owns document projection,
    # while this module owns the database queue.
    from app.services.search import SearchService

    totals = {"claimed": 0, "upserted": 0, "deleted": 0, "batches": 0}
    terminal_status = "idle"
    async with async_session() as db:
        service = SearchService(db)
        for _ in range(max(1, min(int(max_batches), 100))):
            result = await service.drain_search_projection_outbox(
                limit=max(1, min(int(batch_size), OUTBOX_SQL_BATCH_SIZE))
            )
            terminal_status = str(result["status"])
            if terminal_status not in {"ok", "partial"}:
                break
            totals["batches"] += 1
            for key in ("claimed", "upserted", "deleted"):
                totals[key] += int(result[key])
            if terminal_status == "partial":
                # The remainder was released without penalty.  Yield the
                # worker resource permit; the coordinator will wake a fresh
                # <=4 MiB slice instead of monopolizing it in this RQ job.
                break
    return {"status": terminal_status, **totals}


def drain_search_projection_outbox_job(
    batch_size: int = 500,
    max_batches: int = 20,
) -> dict[str, int | str]:
    """Synchronous RQ entry point for the durable search outbox drain."""

    return asyncio.run(drain_search_projection_outbox_batches(
        batch_size=batch_size,
        max_batches=max_batches,
    ))
