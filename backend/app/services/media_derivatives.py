"""Bounded, resumable media derivative processing.

The importer writes authoritative Asset/AssetSource rows and a coalesced outbox
intent in one transaction.  Expensive hashes, non-card thumbnails, video
derivatives and deep reconciliation run here one asset at a time.  No database
transaction is held while libvips, ffmpeg or file hashing is active.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import case, func, not_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models import (
    Asset,
    AssetDedupOutbox,
    AssetSource,
    MediaDerivativeOutbox,
    WorkSource,
)
from app.services.image_utils import can_compute_phash, can_generate_thumbnail
from app.services.media_assets import browser_video_mime_type, render_video_derivatives
from app.services.thumbnail import inspect_and_generate_thumbnail
from app.services.work_import import WorkImportService

logger = logging.getLogger(__name__)

MEDIA_ALGORITHM_VERSION = "media-v1"
PHASH_ALGORITHM_VERSION = "imagehash-phash-v1"
_LEASE_SECONDS = 30 * 60
_REQUEST_SQL_BATCH_SIZE = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _merge_requests(left: dict | None, right: dict | None) -> dict[str, bool]:
    """Treat request JSON as a set: a later false value cannot erase true."""

    merged: dict[str, bool] = {}
    for request in (left or {}, right or {}):
        for key, enabled in request.items():
            if enabled:
                merged[str(key)] = True
    return merged


async def request_media_derivatives(
    db: AsyncSession,
    requests: list[dict[str, Any]],
) -> int:
    """Coalesce at most one durable derivative intent per asset.

    ``requests`` is deliberately a batch API so imports never perform an
    outbox SELECT per asset.  The PostgreSQL upsert is atomic: new work or a
    changed source fingerprint invalidates an older worker lease, while an
    exact duplicate preserves a live claim.  Callers own the transaction.
    """
    if not requests:
        return 0
    # Coalesce duplicate asset ids before building the VALUES list.  In
    # particular, {"video": false} in a later request must not erase an
    # earlier {"video": true} from the same batch.
    by_asset: dict[UUID, dict[str, Any]] = {}
    for item in requests:
        asset_id = UUID(str(item["asset_id"]))
        current = by_asset.get(asset_id)
        requested = _merge_requests(None, item.get("requested"))
        if current is None:
            by_asset[asset_id] = {
                "requested": requested,
                "source_size": item.get("source_size"),
                "source_mtime_ns": item.get("source_mtime_ns"),
            }
            continue
        current["requested"] = _merge_requests(current["requested"], requested)
        # The last observation is the freshest source fingerprint, while the
        # requested work remains the union of every observation.
        current["source_size"] = item.get("source_size")
        current["source_mtime_ns"] = item.get("source_mtime_ns")

    now = _now()
    rows = [
        {
            "id": uuid4(),
            "asset_id": asset_id,
            "requested": item["requested"],
            "algorithm_version": MEDIA_ALGORITHM_VERSION,
            "source_size": item["source_size"],
            "source_mtime_ns": item["source_mtime_ns"],
            "state": "pending",
            "attempts": 0,
            "available_at": now,
        }
        for asset_id, item in by_asset.items()
    ]
    for offset in range(0, len(rows), _REQUEST_SQL_BATCH_SIZE):
        insert_stmt = pg_insert(MediaDerivativeOutbox).values(
            rows[offset : offset + _REQUEST_SQL_BATCH_SIZE]
        )
        merged_requests = MediaDerivativeOutbox.requested.op("||")(
            insert_stmt.excluded.requested
        )
        # Exact duplicate HTTP requests are idempotent while a live claim is
        # already processing.  New work bits, a new source fingerprint, or any
        # non-processing state still requeue immediately.  This prevents a hot
        # thumbnail endpoint from repeatedly invalidating the same worker.
        should_requeue = or_(
            MediaDerivativeOutbox.state != "processing",
            MediaDerivativeOutbox.lease_expires_at.is_(None),
            MediaDerivativeOutbox.algorithm_version.is_distinct_from(
                insert_stmt.excluded.algorithm_version
            ),
            MediaDerivativeOutbox.source_size.is_distinct_from(
                insert_stmt.excluded.source_size
            ),
            MediaDerivativeOutbox.source_mtime_ns.is_distinct_from(
                insert_stmt.excluded.source_mtime_ns
            ),
            merged_requests.is_distinct_from(MediaDerivativeOutbox.requested),
        )
        await db.execute(
            insert_stmt.on_conflict_do_update(
                index_elements=["asset_id"],
                set_={
                    # JSON objects contain only true values, so JSONB concat is
                    # a set union and cannot clear an earlier true request.
                    "requested": merged_requests,
                    "algorithm_version": insert_stmt.excluded.algorithm_version,
                    "source_size": insert_stmt.excluded.source_size,
                    "source_mtime_ns": insert_stmt.excluded.source_mtime_ns,
                    "state": case(
                        (should_requeue, "pending"),
                        else_=MediaDerivativeOutbox.state,
                    ),
                    "available_at": case(
                        (should_requeue, now),
                        else_=MediaDerivativeOutbox.available_at,
                    ),
                    "completed_at": case(
                        (should_requeue, None),
                        else_=MediaDerivativeOutbox.completed_at,
                    ),
                    # Clearing the lease invalidates the exact claim token held
                    # by an older worker.  Its completion/failure CAS is then a
                    # no-op instead of overwriting this newer intent.
                    "lease_expires_at": case(
                        (should_requeue, None),
                        else_=MediaDerivativeOutbox.lease_expires_at,
                    ),
                    "last_error": case(
                        (should_requeue, None),
                        else_=MediaDerivativeOutbox.last_error,
                    ),
                    "updated_at": func.now(),
                },
            )
        )
    return len(by_asset)


async def _claim_one(asset_id: UUID | None = None) -> dict[str, Any] | None:
    async with async_session() as db:
        now = _now()
        conditions = [
            or_(
                (
                    MediaDerivativeOutbox.state.in_(("pending", "failed"))
                    & (MediaDerivativeOutbox.available_at <= now)
                ),
                (
                    (MediaDerivativeOutbox.state == "processing")
                    & (MediaDerivativeOutbox.lease_expires_at < now)
                ),
            )
        ]
        if asset_id is not None:
            conditions.append(MediaDerivativeOutbox.asset_id == asset_id)
        row = (
            await db.execute(
                select(MediaDerivativeOutbox)
                .where(*conditions)
                .order_by(MediaDerivativeOutbox.available_at, MediaDerivativeOutbox.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.state = "processing"
        row.attempts += 1
        lease_token = now + timedelta(seconds=_LEASE_SECONDS)
        row.lease_expires_at = lease_token
        payload = {
            "id": row.id,
            "asset_id": row.asset_id,
            "requested": dict(row.requested or {}),
            "algorithm_version": row.algorithm_version,
            "source_size": row.source_size,
            "source_mtime_ns": row.source_mtime_ns,
            "attempts": row.attempts,
            # This precise timestamp doubles as the claim version. Requeueing
            # or reclaiming changes it, so a stale worker cannot commit.
            "lease_expires_at": lease_token,
        }
        await db.commit()
        return payload


def _video_candidate_condition():
    requested_video = (
        func.coalesce(
            MediaDerivativeOutbox.requested.op("->>")("video"),
            "false",
        )
        == "true"
    )
    mime_type = func.lower(func.coalesce(Asset.mime_type, ""))
    file_path = func.lower(func.coalesce(Asset.file_path, ""))
    return or_(
        requested_video,
        mime_type.like("video/%"),
        file_path.like("%.mp4"),
        file_path.like("%.webm"),
    )


async def _next_resource_candidate() -> tuple[UUID, str] | None:
    """Choose the oldest currently admissible image/video intent.

    Looking at one candidate of each profile prevents a large-video intent
    from permanently blocking smaller image work when only the 256 MiB
    reservation fits above the host hard floor.  This is a read-only hint; the
    exact row is still claimed with ``SKIP LOCKED`` after admission.
    """

    now = _now()
    video_condition = _video_candidate_condition()
    candidates: list[tuple[UUID, str, datetime]] = []
    async with async_session() as db:
        for workload, condition in (
            ("image_derive", not_(video_condition)),
            ("video_derive", video_condition),
        ):
            row = (
                await db.execute(
                    select(
                        MediaDerivativeOutbox.asset_id,
                        MediaDerivativeOutbox.available_at,
                    )
                    .join(Asset, Asset.id == MediaDerivativeOutbox.asset_id)
                    .where(
                        or_(
                            (
                                MediaDerivativeOutbox.state.in_(("pending", "failed"))
                                & (MediaDerivativeOutbox.available_at <= now)
                            ),
                            (
                                (MediaDerivativeOutbox.state == "processing")
                                & (MediaDerivativeOutbox.lease_expires_at < now)
                            ),
                        ),
                        condition,
                    )
                    .order_by(
                        MediaDerivativeOutbox.available_at,
                        MediaDerivativeOutbox.id,
                    )
                    .limit(1)
                )
            ).first()
            if row is not None:
                candidates.append((row.asset_id, workload, row.available_at))
    if not candidates:
        return None

    from app.services.resource_pressure import (
        get_resource_pressure_snapshot,
        resource_profile_permit,
    )

    snapshot = await get_resource_pressure_snapshot()
    admissible = [
        candidate
        for candidate in candidates
        if resource_profile_permit(snapshot, candidate[1])[0]
    ]
    if not admissible:
        # Do not occupy the single operations worker while waiting on a video
        # reservation.  The durable row remains pending and the 15-second
        # outbox coordinator will re-evaluate both profiles; a newly arrived
        # image can therefore make progress first.
        return None
    selected = min(admissible, key=lambda item: item[2])
    return selected[0], selected[1]


async def _load_asset_descriptor(asset_id: UUID) -> dict[str, Any] | None:
    async with async_session() as db:
        row = (
            await db.execute(
                select(Asset, WorkSource)
                .join(AssetSource, AssetSource.asset_id == Asset.id)
                .join(WorkSource, WorkSource.id == AssetSource.work_source_id)
                .where(Asset.id == asset_id)
                .order_by(AssetSource.ordinal, AssetSource.id)
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        asset, work_source = row
        _creator_dir, library_dir = WorkImportService.library_directory(
            work_source.source,
            dict(work_source.raw_metadata or {}),
            work_source.source_work_id,
        )
        return {
            "asset_id": asset.id,
            "file_path": asset.file_path,
            "file_name": asset.file_name,
            "mime_type": asset.mime_type,
            "width": asset.width,
            "height": asset.height,
            "duration": asset.duration,
            "sha256": asset.sha256,
            "phash": asset.phash,
            "thumb_sm_path": asset.thumb_sm_path,
            "thumb_lg_path": asset.thumb_lg_path,
            "derivative_version": asset.derivative_version,
            "derivative_source_size": asset.derivative_source_size,
            "derivative_source_mtime_ns": asset.derivative_source_mtime_ns,
            "library_dir": str(library_dir),
        }


def _safe_source_path(relative: str) -> Path:
    root = Path(settings.download_root).resolve()
    path = (root / relative).resolve()
    path.relative_to(root)
    return path


def _safe_derivative_path(relative: str) -> Path:
    root = Path(settings.library_root).resolve()
    relative_path = Path(relative)
    if relative_path.is_absolute():
        raise ValueError("derivative path must be relative to the library root")
    path = (root / relative_path).resolve()
    path.relative_to(root)
    return path


def _derivative_file_ready(relative: Any) -> bool:
    if not isinstance(relative, str) or not relative:
        return False
    try:
        path = _safe_derivative_path(relative)
        return path.is_file() and path.stat().st_size > 0
    except (OSError, ValueError):
        return False


def _effective_value(
    descriptor: dict[str, Any], values: dict[str, Any], key: str
) -> Any:
    return values[key] if key in values else descriptor.get(key)


def _validate_requested_result(
    descriptor: dict[str, Any], request: dict[str, Any], values: dict[str, Any]
) -> None:
    """Reject incomplete derivatives before the outbox can be completed."""

    requested = request["requested"]
    thumb_sm = _effective_value(descriptor, values, "thumb_sm_path")
    thumb_lg = _effective_value(descriptor, values, "thumb_lg_path")
    if requested.get("thumbnail") and not _derivative_file_ready(thumb_sm):
        raise RuntimeError("requested thumbnail was not produced or is empty")
    if requested.get("video"):
        if not _derivative_file_ready(thumb_sm):
            raise RuntimeError("requested video thumbnail was not produced or is empty")
        if not _derivative_file_ready(thumb_lg):
            raise RuntimeError("requested video poster was not produced or is empty")
    if requested.get("dimensions"):
        width = _effective_value(descriptor, values, "width")
        height = _effective_value(descriptor, values, "height")
        if not isinstance(width, int) or width <= 0:
            raise RuntimeError("requested media width was not produced")
        if not isinstance(height, int) or height <= 0:
            raise RuntimeError("requested media height was not produced")
    if requested.get("sha256") or requested.get("dedup"):
        if not _effective_value(descriptor, values, "sha256"):
            raise RuntimeError("requested sha256 was not produced")
    if requested.get("phash") and not _effective_value(
        descriptor, values, "phash"
    ):
        raise RuntimeError("requested perceptual hash was not produced")


def _derive_sync(descriptor: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    source = _safe_source_path(descriptor["file_path"])
    stat = source.stat()
    expected_size = request.get("source_size")
    expected_mtime = request.get("source_mtime_ns")
    if expected_size is not None and stat.st_size != expected_size:
        raise RuntimeError("asset size changed after derivative intent was recorded")
    if expected_mtime is not None and stat.st_mtime_ns != expected_mtime:
        raise RuntimeError("asset mtime changed after derivative intent was recorded")

    requested = request["requested"]
    library_dir = Path(descriptor["library_dir"])
    library_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "derivative_version": request["algorithm_version"],
        "derivative_source_size": stat.st_size,
        "derivative_source_mtime_ns": stat.st_mtime_ns,
    }
    fingerprint_matches = (
        descriptor.get("derivative_version") == request["algorithm_version"]
        and descriptor.get("derivative_source_size") == stat.st_size
        and descriptor.get("derivative_source_mtime_ns") == stat.st_mtime_ns
    )
    suffix = source.suffix.lower()
    image_derivative_missing = (
        requested.get("thumbnail")
        and not _derivative_file_ready(descriptor.get("thumb_sm_path"))
    ) or (
        requested.get("dimensions")
        and (not descriptor.get("width") or not descriptor.get("height"))
    )
    video_derivative_missing = image_derivative_missing or (
        requested.get("video")
        and (
            not _derivative_file_ready(descriptor.get("thumb_sm_path"))
            or not _derivative_file_ready(descriptor.get("thumb_lg_path"))
        )
    )
    if can_generate_thumbnail(suffix) and (
        not fingerprint_matches or image_derivative_missing
    ) and (requested.get("thumbnail") or requested.get("dimensions")):
        thumb, width, height = inspect_and_generate_thumbnail(
            str(source), library_dir, f"{source.stem}.thumbnail"
        )
        if width is not None:
            result["width"] = width
        if height is not None:
            result["height"] = height
        if thumb:
            result["thumb_sm_path"] = str(
                Path(thumb).relative_to(Path(settings.library_root))
            )
    elif suffix in {".mp4", ".webm"} and (
        not fingerprint_matches or video_derivative_missing
    ) and (
        requested.get("thumbnail")
        or requested.get("dimensions")
        or requested.get("video")
    ):
        derivatives = render_video_derivatives(
            source,
            library_dir,
            source.stem,
            force=not fingerprint_matches or video_derivative_missing,
        )
        if derivatives.inspection:
            result.update(
                width=derivatives.inspection.width,
                height=derivatives.inspection.height,
                duration=derivatives.inspection.duration,
                mime_type=browser_video_mime_type(
                    descriptor["file_name"], descriptor["mime_type"]
                ),
            )
        if derivatives.thumbnail_path:
            result["thumb_sm_path"] = str(
                derivatives.thumbnail_path.relative_to(Path(settings.library_root))
            )
        if derivatives.poster_path:
            result["thumb_lg_path"] = str(
                derivatives.poster_path.relative_to(Path(settings.library_root))
            )
        if derivatives.error:
            raise RuntimeError(derivatives.error)

    if (requested.get("sha256") or requested.get("dedup")) and (
        not fingerprint_matches or not descriptor.get("sha256")
    ):
        result["sha256"] = WorkImportService.sha256(source)
    if (
        requested.get("phash")
        and (not fingerprint_matches or not descriptor.get("phash"))
        and can_compute_phash(suffix)
    ):
        # Preserve the deployed imagehash algorithm exactly; only its scheduling
        # changes. ``phash_version`` prevents future algorithms from cross-
        # comparing incompatible values.
        result["phash"] = WorkImportService.phash(source)
        result["phash_version"] = PHASH_ALGORITHM_VERSION
    _validate_requested_result(descriptor, request, result)
    return result


async def _complete(request: dict[str, Any], values: dict[str, Any]) -> bool:
    async with async_session() as db:
        claim = await db.execute(
            update(MediaDerivativeOutbox)
            .where(
                MediaDerivativeOutbox.id == request["id"],
                MediaDerivativeOutbox.state == "processing",
                MediaDerivativeOutbox.lease_expires_at
                == request["lease_expires_at"],
            )
            .values(
                state="complete",
                completed_at=_now(),
                lease_expires_at=None,
                last_error=None,
            )
            .returning(MediaDerivativeOutbox.id)
        )
        if claim.scalar_one_or_none() is None:
            await db.rollback()
            return False
        if values:
            await db.execute(
                update(Asset)
                .where(Asset.id == request["asset_id"])
                .values(**values)
            )
        if request["requested"].get("dedup"):
            # Deep reconciliation is a separate bounded resource profile.  Its
            # intent must nevertheless be atomic with the hash/derivative
            # update; a post-commit best-effort call can lose the only request
            # when Redis, PostgreSQL, or the worker disappears in that window.
            observed_size = values.get(
                "derivative_source_size", request.get("source_size")
            )
            observed_mtime_ns = values.get(
                "derivative_source_mtime_ns", request.get("source_mtime_ns")
            )
            fingerprint = (
                f"{request['algorithm_version']}:"
                f"{observed_size}:{observed_mtime_ns}"
            )
            await db.execute(
                pg_insert(AssetDedupOutbox)
                .values(
                    id=uuid4(),
                    idempotency_key=(
                        f"asset-dedup:observe:{request['asset_id']}:{fingerprint}"
                    )[:255],
                    event_type="observe",
                    payload={
                        "asset_id": str(request["asset_id"]),
                        "algorithm_version": request["algorithm_version"],
                        "source_size": observed_size,
                        "source_mtime_ns": observed_mtime_ns,
                    },
                    state="pending",
                    attempts=0,
                    available_at=_now(),
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
            )
        await db.commit()
        return True


async def _fail(request: dict[str, Any], exc: Exception) -> bool:
    async with async_session() as db:
        claim = await db.execute(
            update(MediaDerivativeOutbox)
            .where(
                MediaDerivativeOutbox.id == request["id"],
                MediaDerivativeOutbox.state == "processing",
                MediaDerivativeOutbox.lease_expires_at
                == request["lease_expires_at"],
            )
            .values(
                state="failed",
                lease_expires_at=None,
                last_error=str(exc)[:4000],
                available_at=_now()
                + timedelta(
                    seconds=min(3600, 15 * (2 ** min(request["attempts"], 8)))
                ),
            )
            .returning(MediaDerivativeOutbox.id)
        )
        if claim.scalar_one_or_none() is None:
            await db.rollback()
            return False
        await db.commit()
        return True


async def _process_claimed_request(request: dict[str, Any]) -> str:
    """Return processed/failed/stale for one exact CAS-owned request."""

    try:
        descriptor = await _load_asset_descriptor(request["asset_id"])
        if descriptor is None:
            return "processed" if await _complete(request, {}) else "stale"
        values = await asyncio.to_thread(_derive_sync, descriptor, request)
        completed = await _complete(request, values)
    except Exception as exc:
        logger.warning(
            "Media derivative failed for asset %s",
            request["asset_id"],
            exc_info=True,
        )
        return "failed" if await _fail(request, exc) else "stale"
    return "processed" if completed else "stale"


async def process_media_derivative_outbox(
    *,
    limit: int = 25,
    max_seconds: float = 20.0,
    cooldown_result: dict[str, float] | None = None,
) -> dict[str, int | bool]:
    """Process a bounded slice and yield the resource permit promptly."""
    processed = 0
    failed = 0
    claimed = 0
    found_empty = False
    resource_deferred = False
    started = monotonic()
    bounded_limit = max(1, limit)
    bounded_seconds = max(0.001, float(max_seconds))
    while claimed < bounded_limit and monotonic() - started < bounded_seconds:
        candidate = await _next_resource_candidate()
        if candidate is None:
            found_empty = True
            break
        asset_id, workload = candidate
        from app.services.heavy_io import adaptive_resource_slice

        async with adaptive_resource_slice(
            workload,
            f"media-derivative:{asset_id}",
            max_work_units=1,
            max_slice_seconds=bounded_seconds,
            wait_for_capacity=False,
            cooldown_result=cooldown_result,
        ) as limits:
            if limits is None:
                # Aggregate reservations can change after the read-only
                # candidate hint.  Do not pin the only operations worker to a
                # now-ungrantable video; leave the row pending and let the
                # health wake-up reconsider image/video candidates together.
                resource_deferred = True
                break
            # The read-only candidate may have been consumed while admission
            # waited.  Claim that exact asset after the permit is held; never
            # take an unrelated video while holding an image reservation.
            request = await _claim_one(asset_id)
            if request is None:
                continue
            claimed += 1
            outcome = await _process_claimed_request(request)
            if outcome == "processed":
                processed += 1
            elif outcome == "failed":
                failed += 1
    slice_exhausted = not found_empty and not resource_deferred and (
        claimed >= bounded_limit or monotonic() - started >= bounded_seconds
    )
    result: dict[str, int | bool] = {
        "claimed": claimed,
        "processed": processed,
        "failed": failed,
        "slice_exhausted": slice_exhausted,
        "more_likely": slice_exhausted,
    }
    if resource_deferred:
        result["resource_deferred"] = True
    return result


async def media_derivative_status(
    db: AsyncSession,
    asset_ids: list[UUID],
) -> dict[UUID, str]:
    if not asset_ids:
        return {}
    rows = await db.execute(
        select(MediaDerivativeOutbox.asset_id, MediaDerivativeOutbox.state).where(
            MediaDerivativeOutbox.asset_id.in_(asset_ids)
        )
    )
    return {asset_id: state for asset_id, state in rows}
