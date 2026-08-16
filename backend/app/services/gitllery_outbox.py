"""Durable two-stage Gitllery v1 segment projection coordination."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import exists, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import settings
from app.database import async_session
from app.models import (
    CurationChange,
    CurationCommit,
    GitlleryProjectionOutbox,
    GitlleryProjectionTarget,
    GitlleryRepositoryState,
)
from app.services.gitllery.slicing import RepoDescriptor, RepoResolver
from gitllery_format import SegmentRepository

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def request_gitllery_projection(
    db: AsyncSession,
    commit_id: UUID,
    *,
    repository_id: UUID | None = None,
) -> GitlleryProjectionOutbox:
    existing = (
        await db.execute(
            select(GitlleryProjectionOutbox).where(
                GitlleryProjectionOutbox.commit_id == commit_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.state == "failed":
            existing.state = "pending"
            existing.available_at = _now()
            existing.last_error = None
        return existing
    row = GitlleryProjectionOutbox(
        commit_id=commit_id,
        repository_id=repository_id,
        state="pending",
        available_at=_now(),
    )
    db.add(row)
    await db.flush()
    return row


async def _claim_intent_for_planning() -> dict | None:
    async with async_session() as db:
        now = _now()
        has_targets = exists(
            select(GitlleryProjectionTarget.id).where(
                GitlleryProjectionTarget.intent_id == GitlleryProjectionOutbox.id
            )
        )
        row = (
            await db.execute(
                select(GitlleryProjectionOutbox)
                .join(CurationCommit, CurationCommit.id == GitlleryProjectionOutbox.commit_id)
                .where(~has_targets)
                .where(
                    or_(
                        GitlleryProjectionOutbox.state.in_(["pending", "failed"]),
                        (
                            (GitlleryProjectionOutbox.state == "processing")
                            & (GitlleryProjectionOutbox.lease_expires_at < now)
                        ),
                    )
                )
                .where(GitlleryProjectionOutbox.available_at <= now)
                .order_by(CurationCommit.created_at, CurationCommit.id)
                .with_for_update(skip_locked=True, of=GitlleryProjectionOutbox)
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.state = "processing"
        row.attempts += 1
        row.lease_expires_at = now + timedelta(minutes=5)
        payload = {
            "id": row.id,
            "commit_id": row.commit_id,
            "attempts": row.attempts,
            "lease_expires_at": row.lease_expires_at,
        }
        await db.commit()
        return payload


async def _plan_intent(claim: dict) -> int:
    async with async_session() as db:
        commit = await db.get(CurationCommit, claim["commit_id"])
        if commit is None:
            raise RuntimeError("curation commit disappeared while planning")
        changes = list(
            (
                await db.execute(
                    select(CurationChange)
                    .where(CurationChange.commit_id == commit.id)
                    .order_by(
                        CurationChange.sequence.asc().nulls_last(),
                        CurationChange.created_at,
                        CurationChange.id,
                    )
                )
            ).scalars()
        )
        resolver = RepoResolver(db)
        await resolver.preload_work_sources(
            sorted(
                {
                    change.subject_id
                    for change in changes
                    if change.subject_type == "work"
                }
            )
        )
        sliced = await resolver.slice_changes(changes)
        values: list[dict] = []
        for repository_key, (descriptor, repo_changes) in sliced.items():
            payload = _segment_commit_payload(commit, repo_changes)
            values.append(
                {
                    "id": uuid4(),
                    "intent_id": claim["id"],
                    "commit_id": commit.id,
                    "commit_created_at": commit.created_at,
                    "repository_key": repository_key,
                    "source": descriptor.source,
                    "creator_dir": descriptor.creator_dir,
                    "payload": payload,
                    "state": "pending",
                    "attempts": 0,
                    "available_at": _now(),
                }
            )
        if values:
            await db.execute(
                pg_insert(GitlleryProjectionTarget)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=[
                        GitlleryProjectionTarget.commit_id,
                        GitlleryProjectionTarget.repository_key,
                    ]
                )
            )
        state = "processing" if values else "complete"
        result = await db.execute(
            update(GitlleryProjectionOutbox)
            .where(
                GitlleryProjectionOutbox.id == claim["id"],
                GitlleryProjectionOutbox.state == "processing",
                GitlleryProjectionOutbox.attempts == claim["attempts"],
                GitlleryProjectionOutbox.lease_expires_at
                == claim["lease_expires_at"],
            )
            .values(
                state=state,
                lease_expires_at=None,
                completed_at=_now() if not values else None,
                last_error=None,
                projection_stats={"targets": len(values), "format": "gitllery-segment", "revision": 1},
            )
            .returning(GitlleryProjectionOutbox.id)
        )
        if result.scalar_one_or_none() is None:
            await db.rollback()
            return 0
        await db.commit()
        return len(values)


async def _fail_intent(claim: dict, exc: Exception) -> None:
    async with async_session() as db:
        await db.execute(
            update(GitlleryProjectionOutbox)
            .where(
                GitlleryProjectionOutbox.id == claim["id"],
                GitlleryProjectionOutbox.state == "processing",
                GitlleryProjectionOutbox.attempts == claim["attempts"],
                GitlleryProjectionOutbox.lease_expires_at
                == claim["lease_expires_at"],
            )
            .values(
                state="failed",
                lease_expires_at=None,
                last_error=str(exc)[:4000],
                available_at=_now()
                + timedelta(seconds=min(3600, 15 * (2 ** min(claim["attempts"], 8)))),
            )
        )
        await db.commit()


def _segment_commit_payload(
    commit: CurationCommit,
    changes: list[CurationChange],
) -> dict:
    return {
        "commit_id": str(commit.id),
        "parent_commit_id": None,
        "authoritative_parent_commit_id": (
            str(commit.parent_commit_id) if commit.parent_commit_id else None
        ),
        "actor_type": commit.actor_type,
        "actor_id": commit.actor_id,
        "message": commit.message,
        "trigger": commit.trigger,
        "dedupe_key": commit.dedupe_key,
        "reverts_commit_id": (
            str(commit.reverts_commit_id) if commit.reverts_commit_id else None
        ),
        "occurred_at": commit.occurred_at.isoformat() if commit.occurred_at else None,
        "created_at": commit.created_at.isoformat(),
        "stats": commit.stats or {},
        "metadata": commit.extra_metadata or {},
        "changes": [
            {
                "change_id": str(change.id),
                "subject_type": change.subject_type,
                "subject_id": change.subject_id,
                "action": change.action,
                "sequence": change.sequence,
                "before_state": change.before_state,
                "after_state": change.after_state,
                "diff": change.diff or {},
                "impact": change.impact or {},
            }
            for change in changes
        ],
    }


async def _claim_target() -> dict | None:
    async with async_session() as db:
        now = _now()
        earlier = aliased(GitlleryProjectionTarget)
        has_earlier = exists(
            select(earlier.id).where(
                earlier.repository_key == GitlleryProjectionTarget.repository_key,
                earlier.state != "complete",
                tuple_(earlier.commit_created_at, earlier.commit_id)
                < tuple_(
                    GitlleryProjectionTarget.commit_created_at,
                    GitlleryProjectionTarget.commit_id,
                ),
            )
        )
        row = (
            await db.execute(
                select(GitlleryProjectionTarget)
                .where(~has_earlier)
                .where(
                    or_(
                        GitlleryProjectionTarget.state.in_(["pending", "failed"]),
                        (
                            (GitlleryProjectionTarget.state == "processing")
                            & (GitlleryProjectionTarget.lease_expires_at < now)
                        ),
                    )
                )
                .where(GitlleryProjectionTarget.available_at <= now)
                .order_by(
                    GitlleryProjectionTarget.commit_created_at,
                    GitlleryProjectionTarget.commit_id,
                    GitlleryProjectionTarget.id,
                )
                .with_for_update(skip_locked=True, of=GitlleryProjectionTarget)
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        token = uuid4()
        row.state = "processing"
        row.attempts += 1
        row.lease_token = token
        row.lease_expires_at = now + timedelta(minutes=5)
        payload = {
            "id": row.id,
            "intent_id": row.intent_id,
            "commit_id": row.commit_id,
            "commit_created_at": row.commit_created_at,
            "repository_key": row.repository_key,
            "source": row.source,
            "creator_dir": row.creator_dir,
            "payload": row.payload,
            "attempts": row.attempts,
            "lease_token": token,
        }
        await db.commit()
        return payload


def _repository_path(target: dict) -> Path:
    library = Path(settings.library_root).resolve()
    creator_root = (library / target["source"] / target["creator_dir"]).resolve()
    creator_root.relative_to(library)
    mode = settings.gitllery_projection_mode.strip().lower()
    if mode == "active":
        return creator_root / ".gitllery"
    if mode != "shadow":
        raise RuntimeError(f"unsupported Gitllery projection mode: {mode}")
    generation = "".join(
        character
        for character in settings.gitllery_build_generation
        if character.isalnum() or character in {"-", "_"}
    ) or "segment-r1"
    return creator_root / f".gitllery.build-{generation}"


def _write_target(target: dict) -> dict:
    repo = SegmentRepository(_repository_path(target))
    repo.root.relative_to(Path(settings.library_root).resolve())
    with repo.projection_lock():
        repo.initialise(
            repository_id=target["repository_key"],
            source=target["source"],
            creator_dir=target["creator_dir"],
            generation=settings.gitllery_build_generation,
        )
        manifest = repo.read_manifest()
        payload = {
            **target["payload"],
            "parent_commit_id": manifest.get("last_complete_commit_id"),
        }
        return repo.append([payload])


async def _finish_target(target: dict, manifest: dict) -> bool:
    async with async_session() as db:
        result = await db.execute(
            update(GitlleryProjectionTarget)
            .where(
                GitlleryProjectionTarget.id == target["id"],
                GitlleryProjectionTarget.state == "processing",
                GitlleryProjectionTarget.attempts == target["attempts"],
                GitlleryProjectionTarget.lease_token == target["lease_token"],
            )
            .values(
                state="complete",
                completed_at=_now(),
                lease_token=None,
                lease_expires_at=None,
                segment_digest=manifest.get("head_segment"),
                last_error=None,
            )
            .returning(GitlleryProjectionTarget.id)
        )
        if result.scalar_one_or_none() is None:
            await db.rollback()
            return False
        mode = "active" if settings.gitllery_projection_mode == "active" else "shadow"
        await db.execute(
            pg_insert(GitlleryRepositoryState)
            .values(
                id=uuid4(),
                repository_key=target["repository_key"],
                source=target["source"],
                creator_dir=target["creator_dir"],
                product_version="v1",
                format_id="gitllery-segment",
                format_revision=1,
                mode=mode,
                generation=str(manifest.get("generation") or settings.gitllery_build_generation),
                head_segment=manifest.get("head_segment"),
                last_complete_commit_id=target["commit_id"],
                last_complete_created_at=target["commit_created_at"],
                segment_count=int(manifest.get("segment_count") or 0),
                commit_count=int(manifest.get("commit_count") or 0),
                change_count=int(manifest.get("change_count") or 0),
            )
            .on_conflict_do_update(
                index_elements=[GitlleryRepositoryState.repository_key],
                set_={
                    "mode": mode,
                    "generation": str(manifest.get("generation") or settings.gitllery_build_generation),
                    "head_segment": manifest.get("head_segment"),
                    "last_complete_commit_id": target["commit_id"],
                    "last_complete_created_at": target["commit_created_at"],
                    "segment_count": int(manifest.get("segment_count") or 0),
                    "commit_count": int(manifest.get("commit_count") or 0),
                    "change_count": int(manifest.get("change_count") or 0),
                    "last_error": None,
                    "updated_at": _now(),
                },
            )
        )
        await db.commit()
        return True


async def _fail_target(target: dict, exc: Exception) -> None:
    async with async_session() as db:
        await db.execute(
            update(GitlleryProjectionTarget)
            .where(
                GitlleryProjectionTarget.id == target["id"],
                GitlleryProjectionTarget.state == "processing",
                GitlleryProjectionTarget.attempts == target["attempts"],
                GitlleryProjectionTarget.lease_token == target["lease_token"],
            )
            .values(
                state="failed",
                lease_token=None,
                lease_expires_at=None,
                last_error=str(exc)[:4000],
                available_at=_now()
                + timedelta(seconds=min(3600, 15 * (2 ** min(target["attempts"], 8)))),
            )
        )
        await db.commit()


async def _settle_intent(intent_id: UUID) -> None:
    async with async_session() as db:
        incomplete = (
            await db.execute(
                select(func.count(GitlleryProjectionTarget.id)).where(
                    GitlleryProjectionTarget.intent_id == intent_id,
                    GitlleryProjectionTarget.state != "complete",
                )
            )
        ).scalar_one()
        if int(incomplete or 0) == 0:
            await db.execute(
                update(GitlleryProjectionOutbox)
                .where(GitlleryProjectionOutbox.id == intent_id)
                .values(
                    state="complete",
                    completed_at=_now(),
                    lease_expires_at=None,
                    last_error=None,
                )
            )
            await db.commit()


async def process_gitllery_projection_outbox(
    *,
    limit: int = 25,
    max_seconds: float = 20.0,
) -> dict[str, int | bool]:
    processed = failed = claimed = planned = 0
    found_empty = False
    started = monotonic()
    bounded_limit = max(1, min(int(limit), 100))
    bounded_seconds = max(0.001, min(float(max_seconds), 20.0))

    planning = await _claim_intent_for_planning()
    if planning is not None:
        try:
            planned = await _plan_intent(planning)
        except Exception as exc:
            logger.warning("Gitllery intent planning failed", exc_info=True)
            await _fail_intent(planning, exc)
            failed += 1

    while claimed < bounded_limit and monotonic() - started < bounded_seconds:
        target = await _claim_target()
        if target is None:
            found_empty = True
            break
        claimed += 1
        try:
            manifest = await asyncio.to_thread(_write_target, target)
            if await _finish_target(target, manifest):
                processed += 1
            await _settle_intent(target["intent_id"])
        except Exception as exc:
            logger.warning(
                "Gitllery target projection failed for %s/%s",
                target["repository_key"],
                target["commit_id"],
                exc_info=True,
            )
            await _fail_target(target, exc)
            failed += 1
    exhausted = not found_empty and (
        claimed >= bounded_limit or monotonic() - started >= bounded_seconds
    )
    return {
        "planned": planned,
        "claimed": claimed,
        "processed": processed,
        "failed": failed,
        "slice_exhausted": exhausted,
        "more_likely": exhausted or planning is not None,
    }


async def gitllery_outbox_health(db: AsyncSession) -> dict[str, int | float | None]:
    now = _now()
    intent_rows = await db.execute(
        select(
            GitlleryProjectionOutbox.state,
            func.count(GitlleryProjectionOutbox.id),
            func.min(GitlleryProjectionOutbox.created_at),
        ).group_by(GitlleryProjectionOutbox.state)
    )
    target_rows = await db.execute(
        select(
            GitlleryProjectionTarget.state,
            func.count(GitlleryProjectionTarget.id),
            func.min(GitlleryProjectionTarget.created_at),
        ).group_by(GitlleryProjectionTarget.state)
    )
    counts: dict[str, int] = {}
    target_counts: dict[str, int] = {}
    oldest: datetime | None = None
    for state, count, created_at in intent_rows:
        counts[state] = int(count)
        if state != "complete" and created_at and (oldest is None or created_at < oldest):
            oldest = created_at
    for state, count, created_at in target_rows:
        target_counts[state] = int(count)
        if state != "complete" and created_at and (oldest is None or created_at < oldest):
            oldest = created_at
    return {
        "pending": counts.get("pending", 0),
        "processing": counts.get("processing", 0),
        "failed": counts.get("failed", 0),
        "targets_pending": target_counts.get("pending", 0),
        "targets_processing": target_counts.get("processing", 0),
        "targets_failed": target_counts.get("failed", 0),
        "oldest_age_seconds": max(0.0, (now - oldest).total_seconds()) if oldest else None,
    }
