"""RQ jobs for asset reconciliation scans and durable storage effects."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta
from pathlib import Path
from time import monotonic
from uuid import UUID

from sqlalchemy import or_, select, update

from app.database import async_session
from app.models import (
    Asset,
    AssetDedupEvidence,
    AssetDedupOutbox,
    AssetDedupScan,
    AssetStorageState,
    VisualAssetGroup,
    VisualAssetMember,
)
from app.services.asset_dedup_scope import (
    CrossSourceDedupScope,
    scope_error_message,
)
from app.services.asset_reconciliation import (
    IMAGE_MIME_TYPES,
    PHASH_MAX_DISTANCE,
    QUARANTINE_DAYS,
    SSIM_MIN_SCORE,
    AssetReconciliation,
    _safe_download_path,
    _utcnow,
    structural_similarity,
    versioned_phash_distance,
)
from app.services.operations import (
    OPERATION_TTL_SECONDS,
    release_owned_operation_lock,
    set_operation_status,
)
from app.services.heavy_io import adaptive_resource_slice, try_heavy_io_slot
from app.services.outbox_coordinator import (
    clear_and_wake_outbox_successor,
    wake_pending_outboxes,
)
from app.services.resource_pressure import (
    current_profile_slice_limits,
    profile_slice_cooldown_seconds,
)
from app.services.queue_admission import checked_enqueue_in
from app.services.redis_client import get_redis
from app.services.stage_metrics import measure_stage
from app.services.tasks import TaskService
from app.services.work_import import WorkImportService


logger = logging.getLogger(__name__)

ASSET_DEDUP_SCAN_OPERATION_LOCK = "lock:admin:asset-dedup-scan"
ASSET_DEDUP_SCAN_QUEUE = "maintenance"
ASSET_DEDUP_SCAN_FUNCTION = "app.jobs.asset_dedup.run_asset_dedup_scan"
ASSET_DEDUP_SCAN_RESOURCE_RETRY_SECONDS = 10.0
ASSET_DEDUP_SCAN_SLICE_TIMEOUT_SECONDS = 3600

_REFRESH_SCAN_LOCK_SCRIPT = """
local current = redis.call('get', KEYS[1])
if current == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
if not current then
  local installed = redis.call('set', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2])
  if installed then
    return 1
  end
end
return 0
"""


async def _claim_outbox_event() -> dict | None:
    async with async_session() as db:
        now = _utcnow()
        stale_before = now - timedelta(minutes=15)
        event = (
            await db.execute(
                select(AssetDedupOutbox)
                .where(
                    or_(
                        (
                            AssetDedupOutbox.state.in_(("pending", "failed"))
                            & (AssetDedupOutbox.available_at <= now)
                        ),
                        (
                            (AssetDedupOutbox.state == "processing")
                            & (AssetDedupOutbox.updated_at <= stale_before)
                        ),
                    ),
                )
                .order_by(AssetDedupOutbox.available_at, AssetDedupOutbox.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if not event:
            return None
        event.state = "processing"
        event.attempts += 1
        payload = {
            "id": event.id,
            "event_type": event.event_type,
            "payload": dict(event.payload),
            "attempts": event.attempts,
        }
        await db.commit()
        return payload


async def _finish_event(event: dict) -> bool:
    async with async_session() as db:
        result = await db.execute(
            update(AssetDedupOutbox)
            .where(
                AssetDedupOutbox.id == event["id"],
                AssetDedupOutbox.state == "processing",
                AssetDedupOutbox.attempts == event["attempts"],
            )
            .values(
                state="complete",
                completed_at=_utcnow(),
                last_error=None,
            )
            .returning(AssetDedupOutbox.id)
        )
        owned = result.scalar_one_or_none() is not None
        await db.commit()
        return owned


async def _fail_event(event: dict, error: Exception) -> bool:
    async with async_session() as db:
        result = await db.execute(
            update(AssetDedupOutbox)
            .where(
                AssetDedupOutbox.id == event["id"],
                AssetDedupOutbox.state == "processing",
                AssetDedupOutbox.attempts == event["attempts"],
            )
            .values(
                state="failed",
                last_error=str(error)[:4000],
                available_at=_utcnow()
                + timedelta(
                    seconds=min(3600, 30 * (2 ** min(event["attempts"], 7)))
                ),
            )
            .returning(AssetDedupOutbox.id)
        )
        owned = result.scalar_one_or_none() is not None
        await db.commit()
        return owned


async def _load_storage_subjects(payload: dict):
    asset_id = UUID(payload["asset_id"])
    representative_id = UUID(payload["representative_asset_id"])
    group_id = UUID(payload["group_id"])
    if asset_id == representative_id:
        raise ValueError("Representative cannot be its own storage subject")
    async with async_session() as db:
        asset = await db.get(Asset, asset_id)
        representative = await db.get(Asset, representative_id)
        if not asset or not representative:
            raise ValueError("Asset storage subject no longer exists")
        group = await db.get(VisualAssetGroup, group_id)
        if not group or group.representative_asset_id != representative_id:
            raise ValueError(
                "Visual group or representative changed; refusing storage action"
            )
        member_ids = set(
            (
                await db.execute(
                    select(VisualAssetMember.asset_id).where(
                        VisualAssetMember.group_id == group_id
                    )
                )
            ).scalars()
        )
        if asset_id not in member_ids or representative_id not in member_ids:
            raise ValueError(
                "Storage subjects are no longer members of the visual group"
            )
        scope = await CrossSourceDedupScope(db).group(member_ids)
        if not scope.eligible:
            raise ValueError(scope_error_message(scope.reason))
        state = (
            await db.execute(
                select(AssetStorageState).where(
                    AssetStorageState.asset_id == asset_id
                )
            )
        ).scalar_one_or_none()
        if not state:
            state = AssetStorageState(
                asset_id=asset_id,
                storage_state="available",
                bytes_reclaimed=0,
                served_by_asset_id=representative_id,
            )
            db.add(state)
            await db.commit()
        elif state.served_by_asset_id != representative_id:
            raise ValueError(
                "Asset storage representative changed; refusing storage action"
            )
        return asset, representative


def _rehash(path: Path) -> str:
    return WorkImportService.sha256(path)


async def _run_hardlink(event: dict) -> None:
    payload = event["payload"]
    asset, representative = await _load_storage_subjects(payload)
    source_path = _safe_download_path(asset.file_path)
    representative_path = _safe_download_path(representative.file_path)
    if not source_path.exists() or not representative_path.exists():
        raise FileNotFoundError("Hardlink source or representative is missing")
    source_hash, representative_hash = await asyncio.gather(
        asyncio.to_thread(_rehash, source_path),
        asyncio.to_thread(_rehash, representative_path),
    )
    if source_hash != representative_hash:
        raise ValueError("SHA-256 changed before hardlink replacement")

    source_stat = source_path.stat()
    representative_stat = representative_path.stat()
    reclaimed = 0
    if (source_stat.st_dev, source_stat.st_ino) != (
        representative_stat.st_dev,
        representative_stat.st_ino,
    ):
        if source_stat.st_dev != representative_stat.st_dev:
            raise OSError("Assets are on different filesystems; hardlink skipped")
        temporary = source_path.with_name(f".{source_path.name}.dedup-{event['id']}")
        if temporary.exists():
            temporary.unlink()
        os.link(representative_path, temporary)
        os.replace(temporary, source_path)
        reclaimed = int(asset.file_size or source_stat.st_size)

    async with async_session() as db:
        state = (
            await db.execute(
                select(AssetStorageState).where(
                    AssetStorageState.asset_id == asset.id
                )
            )
        ).scalar_one()
        state.storage_state = "hardlinked"
        state.dedup_kind = "exact_sha256"
        state.served_by_asset_id = representative.id
        state.bytes_reclaimed = max(state.bytes_reclaimed, reclaimed)
        state.last_verified_at = _utcnow()
        await db.commit()


async def _run_quarantine(event: dict) -> None:
    payload = event["payload"]
    asset, representative = await _load_storage_subjects(payload)
    source_path = _safe_download_path(asset.file_path)
    representative_path = _safe_download_path(representative.file_path)
    if not representative_path.exists():
        raise FileNotFoundError("Representative file is missing")
    async with async_session() as evidence_db:
        evidence = await evidence_db.get(
            AssetDedupEvidence, UUID(payload["evidence_id"])
        )
        thresholds = ((evidence.facts or {}).get("thresholds") or {}) if evidence else {}
        phash_limit = int(thresholds.get("phash_max_distance", PHASH_MAX_DISTANCE))
        ssim_limit = float(thresholds.get("ssim_min_score", SSIM_MIN_SCORE))
        quarantine_days = int(thresholds.get("quarantine_days", QUARANTINE_DAYS))

    quarantine_relative = (
        Path(".dedup-quarantine") / str(asset.id) / asset.file_name
    )
    quarantine_path = _safe_download_path(str(quarantine_relative))
    candidate_path = source_path if source_path.exists() else quarantine_path
    if not candidate_path.exists():
        raise FileNotFoundError("Candidate file is missing from source and quarantine")
    candidate_hash, representative_hash = await asyncio.gather(
        asyncio.to_thread(_rehash, candidate_path),
        asyncio.to_thread(_rehash, representative_path),
    )
    if asset.sha256 and candidate_hash != asset.sha256:
        raise ValueError("Candidate bytes changed before quarantine")
    if representative.sha256 and representative_hash != representative.sha256:
        raise ValueError("Representative bytes changed before quarantine")
    distance = versioned_phash_distance(
        asset.phash,
        representative.phash,
        asset.phash_version,
        representative.phash_version,
    )
    if distance is None or distance > phash_limit:
        raise ValueError("pHash no longer passes the visual gate")
    ssim = await asyncio.to_thread(
        structural_similarity,
        candidate_path,
        representative_path,
    )
    if ssim < ssim_limit:
        raise ValueError("SSIM no longer passes the visual gate")
    if candidate_path == source_path:
        quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source_path, quarantine_path)

    async with async_session() as db:
        state = (
            await db.execute(
                select(AssetStorageState).where(
                    AssetStorageState.asset_id == asset.id
                )
            )
        ).scalar_one()
        quarantined_at = state.quarantined_at or _utcnow()
        purge_after = state.purge_after or (
            quarantined_at + timedelta(days=quarantine_days)
        )
        state.storage_state = "quarantined"
        state.dedup_kind = "visual"
        state.served_by_asset_id = representative.id
        state.quarantine_path = str(quarantine_relative)
        state.quarantine_sha256 = candidate_hash
        state.quarantined_at = quarantined_at
        state.purge_after = purge_after
        state.last_verified_at = _utcnow()
        purge_key = f"asset-dedup:purge:{asset.id}:{representative.id}"
        existing = (
            await db.execute(
                select(AssetDedupOutbox.id).where(
                    AssetDedupOutbox.idempotency_key == purge_key
                )
            )
        ).scalar_one_or_none()
        if not existing:
            db.add(
                AssetDedupOutbox(
                    idempotency_key=purge_key,
                    event_type="purge",
                    payload=payload,
                    state="pending",
                    attempts=0,
                    available_at=purge_after,
                )
            )
        await db.commit()


async def _run_purge(event: dict) -> None:
    payload = event["payload"]
    asset, representative = await _load_storage_subjects(payload)
    representative_path = _safe_download_path(representative.file_path)
    if not representative_path.exists():
        raise FileNotFoundError("Representative file is missing; refusing purge")
    representative_hash = await asyncio.to_thread(_rehash, representative_path)
    if representative.sha256 and representative_hash != representative.sha256:
        raise ValueError("Representative bytes changed; refusing purge")

    async with async_session() as db:
        state = (
            await db.execute(
                select(AssetStorageState).where(
                    AssetStorageState.asset_id == asset.id
                )
            )
        ).scalar_one()
        if state.storage_state == "purged":
            return
        if state.storage_state != "quarantined" or not state.quarantine_path:
            raise ValueError("Asset is not safely quarantined")
        if state.purge_after and state.purge_after > _utcnow():
            raise ValueError("Quarantine retention has not elapsed")
        quarantine_path = _safe_download_path(state.quarantine_path)
        if not quarantine_path.exists():
            raise FileNotFoundError("Quarantined candidate is missing; refusing purge")
        expected_hash = state.quarantine_sha256 or asset.sha256
        if not expected_hash:
            raise ValueError("Quarantined candidate has no verification hash")
        quarantine_hash = await asyncio.to_thread(_rehash, quarantine_path)
        if quarantine_hash != expected_hash:
            raise ValueError("Quarantined candidate bytes changed; refusing purge")
        reclaimed = int(asset.file_size or quarantine_path.stat().st_size)
        quarantine_path.unlink()
        state.storage_state = "purged"
        state.purged_at = _utcnow()
        state.bytes_reclaimed = max(state.bytes_reclaimed, reclaimed)
        state.missing_files = None
        state.last_verified_at = _utcnow()
        await db.commit()


async def _run_observe(event: dict) -> None:
    """Evaluate one asset from a durable media-completion intent."""

    payload = event["payload"]
    asset_id = UUID(payload["asset_id"])
    async with async_session() as db:
        asset = await db.get(Asset, asset_id)
        if asset is None:
            return
        if (
            asset.derivative_version != payload.get("algorithm_version")
            or asset.derivative_source_size != payload.get("source_size")
            or asset.derivative_source_mtime_ns != payload.get("source_mtime_ns")
        ):
            # A newer media intent owns reconciliation for the current bytes.
            # Completing this stale observation is safe because that newer
            # completion inserted its own fingerprinted outbox key.
            return
        await AssetReconciliation(db).observe([asset_id], auto_apply=True)
        await db.commit()


async def _process_asset_dedup_event(event: dict) -> bool:
    """Process one claimed storage effect while exactly one permit is held."""

    try:
        if event["event_type"] == "observe":
            await _run_observe(event)
        elif event["event_type"] == "hardlink":
            await _run_hardlink(event)
        elif event["event_type"] == "quarantine":
            await _run_quarantine(event)
        elif event["event_type"] == "purge":
            await _run_purge(event)
        else:
            raise ValueError(f"Unknown asset dedup event: {event['event_type']}")
    except Exception as exc:
        await _fail_event(event, exc)
        return False
    return await _finish_event(event)


async def process_asset_dedup_outbox(
    *, limit: int = 100
) -> dict[str, int | bool | float]:
    """Drain storage effects one resource permit at a time.

    The operations worker deliberately classifies this coordinator as
    ``light``.  Each filesystem mutation therefore acquires its own image
    profile slice here, and releases it before the next outbox row is claimed.
    Resource waiting never holds a database transaction or a previously
    claimed event lease.
    """

    processed = 0
    failed = 0
    successor_delay_seconds = 0.0
    bounded_limit = max(1, min(int(limit), 100))
    while processed + failed < bounded_limit:
        owner = f"asset-dedup-outbox:{os.getpid()}:{processed + failed}"
        limits, pressure_snapshot = await current_profile_slice_limits(
            "asset-dedup-outbox",
            max_work_units=1,
            max_slice_seconds=20.0,
        )
        if not limits.allowed:
            break
        slice_started = monotonic()
        completed_slice = False
        async with try_heavy_io_slot("asset-dedup-outbox", owner) as lease:
            if lease is None:
                break
            event = await _claim_outbox_event()
            if event is None:
                break
            if await _process_asset_dedup_event(event):
                processed += 1
            else:
                failed += 1
            completed_slice = True
        if completed_slice:
            successor_delay_seconds = profile_slice_cooldown_seconds(
                pressure_snapshot,
                elapsed_seconds=monotonic() - slice_started,
                max_seconds=300.0,
                workload="asset-dedup-outbox",
            )
            # One image mutation per RQ coordinator. A delayed successor keeps
            # AIMD accurate without sleeping in the only operations worker.
            break
    return {
        "processed": processed,
        "failed": failed,
        "more_likely": bool(processed or failed),
        "successor_delay_seconds": successor_delay_seconds,
    }


def _scan_generation(options: dict | None) -> int:
    try:
        return max(0, int((options or {}).get("_rq_generation", 0)))
    except (TypeError, ValueError):
        return 0


def _scan_payload(
    scan: AssetDedupScan,
    *,
    status: str | None = None,
    resource_state: str | None = None,
    resource_reason: str | None = None,
    successor_delay_seconds: float = 0.0,
) -> dict:
    return {
        "scan_id": str(scan.id),
        "status": status or scan.status,
        "assets_scanned": int(scan.assets_scanned or 0),
        "candidates_evaluated": int(scan.candidates_evaluated or 0),
        "cases_created": int(scan.cases_created or 0),
        "assets_grouped": int(scan.assets_grouped or 0),
        "bytes_reclaimable": int(scan.bytes_reclaimable or 0),
        "resource_state": resource_state,
        "resource_reason": resource_reason,
        "successor_delay_seconds": round(max(0.0, successor_delay_seconds), 3),
        "generation": _scan_generation(scan.options),
    }


def _refresh_owned_scan_operation_lock(operation_job_id: str) -> bool:
    """Renew only the original operation's single-flight ownership."""

    redis = get_redis()
    try:
        return bool(
            redis.eval(
                _REFRESH_SCAN_LOCK_SCRIPT,
                1,
                ASSET_DEDUP_SCAN_OPERATION_LOCK,
                operation_job_id,
                OPERATION_TTL_SECONDS,
            )
        )
    except Exception:
        # Keep compatibility with small test Redis doubles that lack EVAL.  A
        # real Redis connection failure still propagates from GET/EXPIRE and is
        # made terminal by the durable failure path below.
        current = redis.get(ASSET_DEDUP_SCAN_OPERATION_LOCK)
        if isinstance(current, bytes):
            current = current.decode()
        if current != operation_job_id:
            return False
        return bool(redis.expire(ASSET_DEDUP_SCAN_OPERATION_LOCK, OPERATION_TTL_SECONDS))


async def _run_scan(
    scan_id: UUID,
    *,
    operation_job_id: str,
    expected_generation: int,
) -> dict:
    """Run at most one image-derived scan slice and persist its keyset cursor."""

    async with async_session() as db:
        scan = (
            await db.execute(
                select(AssetDedupScan)
                .where(AssetDedupScan.id == scan_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if scan is None:
            raise KeyError("Asset dedup scan not found")
        scan_options = dict(scan.options or {})
        current_generation = _scan_generation(scan_options)
        persisted_owner = scan_options.get("_operation_job_id")
        if current_generation != expected_generation or (
            persisted_owner is not None
            and str(persisted_owner) != operation_job_id
        ):
            return _scan_payload(
                scan,
                status="superseded",
                resource_state="yielded",
                resource_reason="newer_scan_slice_owns_cursor",
            )
        if scan.status in {"complete", "failed"}:
            return _scan_payload(scan, resource_state="yielded")
        scan_options["_operation_job_id"] = operation_job_id
        scan_options["_rq_generation"] = current_generation
        scan.options = scan_options
        scan.status = "running"
        scan.error = None
        configured_batch_size = max(
            1,
            min(int(scan_options.get("batch_size", 100)), 100),
        )
        await db.commit()

    if not await asyncio.to_thread(
        _refresh_owned_scan_operation_lock,
        operation_job_id,
    ):
        raise RuntimeError("Asset dedup scan operation ownership was lost")

    cooldown: dict[str, float] = {}
    resource_unavailable = False
    outcome: dict | None = None
    owner = f"asset-dedup-scan:{scan_id}:{expected_generation}"
    async with adaptive_resource_slice(
        "image_derive",
        owner,
        max_work_units=configured_batch_size,
        max_slice_seconds=20.0,
        wait_for_capacity=False,
        cooldown_result=cooldown,
    ) as limits:
        if limits is None:
            resource_unavailable = True
        else:
            # The generation is checked again while the cursor row is locked.
            # A duplicate RQ delivery can therefore never apply the same asset
            # range or publish a second valid successor.
            async with async_session() as db:
                scan = (
                    await db.execute(
                        select(AssetDedupScan)
                        .where(AssetDedupScan.id == scan_id)
                        .with_for_update()
                    )
                ).scalar_one()
                scan_options = dict(scan.options or {})
                if (
                    _scan_generation(scan_options) != expected_generation
                    or str(scan_options.get("_operation_job_id"))
                    != operation_job_id
                ):
                    outcome = _scan_payload(
                        scan,
                        status="superseded",
                        resource_state="yielded",
                        resource_reason="newer_scan_slice_owns_cursor",
                    )
                elif scan.status in {"complete", "failed"}:
                    outcome = _scan_payload(scan, resource_state="yielded")
                else:
                    batch_size = max(
                        1,
                        min(configured_batch_size, int(limits.work_units)),
                    )
                    query = (
                        select(Asset.id)
                        .where(
                            Asset.mime_type.in_(IMAGE_MIME_TYPES),
                            or_(Asset.sha256.isnot(None), Asset.phash.isnot(None)),
                        )
                        .order_by(Asset.id)
                        .limit(batch_size)
                    )
                    if scan.cursor_asset_id:
                        query = query.where(Asset.id > scan.cursor_asset_id)
                    asset_ids = list((await db.execute(query)).scalars())
                    if not asset_ids:
                        scan.status = "complete"
                        await db.commit()
                        outcome = _scan_payload(
                            scan,
                            resource_state="yielded",
                        )
                    else:
                        result = await AssetReconciliation(db).observe(
                            asset_ids,
                            auto_apply=bool(scan_options.get("auto_apply", True)),
                        )
                        scan.cursor_asset_id = asset_ids[-1]
                        scan.assets_scanned += result["assets_scanned"]
                        scan.candidates_evaluated += result["candidates_evaluated"]
                        scan.cases_created += result["cases_created"]
                        scan.assets_grouped += result["assets_grouped"]
                        scan.bytes_reclaimable += result["bytes_reclaimable"]
                        await db.commit()
                        outcome = _scan_payload(
                            scan,
                            resource_state="yielded",
                            resource_reason="adaptive_cooldown",
                        )

    if resource_unavailable:
        async with async_session() as db:
            scan = await db.get(AssetDedupScan, scan_id)
            if scan is None:
                raise KeyError("Asset dedup scan not found")
            return _scan_payload(
                scan,
                resource_state="waiting",
                resource_reason="resource_capacity",
                successor_delay_seconds=ASSET_DEDUP_SCAN_RESOURCE_RETRY_SECONDS,
            )

    assert outcome is not None
    if outcome["status"] == "running":
        outcome["successor_delay_seconds"] = round(
            max(1.0, float(cooldown.get("seconds", 0.0))),
            3,
        )
    return outcome


async def _reserve_scan_successor(
    scan_id: UUID,
    *,
    operation_job_id: str,
    expected_generation: int,
) -> dict | None:
    """Fence one deterministic successor in the durable scan row."""

    async with async_session() as db:
        scan = (
            await db.execute(
                select(AssetDedupScan)
                .where(AssetDedupScan.id == scan_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if scan is None or scan.status != "running":
            return None
        scan_options = dict(scan.options or {})
        if (
            _scan_generation(scan_options) != expected_generation
            or str(scan_options.get("_operation_job_id")) != operation_job_id
        ):
            return None
        next_generation = expected_generation + 1
        rq_job_id = (
            f"asset-dedup-scan-{scan_id.hex}-{next_generation:08x}"
        )
        scan_options["_rq_generation"] = next_generation
        scan_options["_next_rq_job_id"] = rq_job_id
        scan.options = scan_options
        await db.commit()
        return {
            "generation": next_generation,
            "rq_job_id": rq_job_id,
        }


def _enqueue_scan_successor(
    operation_job_id: str,
    scan_id: UUID,
    successor: dict,
    delay_seconds: float,
) -> None:
    """Publish one unique delayed successor, tolerating an acknowledged retry."""

    from rq import Queue
    from rq.exceptions import NoSuchJobError
    from rq.job import Job

    redis = get_redis()
    rq_job_id = str(successor["rq_job_id"])
    try:
        existing = Job.fetch(rq_job_id, connection=redis)
    except NoSuchJobError:
        pass
    else:
        raw_status = existing.get_status(refresh=True)
        status = str(getattr(raw_status, "value", raw_status)).lower()
        if status in {"failed", "stopped", "canceled", "cancelled"}:
            raise RuntimeError(
                f"Asset dedup successor {rq_job_id} is already {status}"
            )
        return

    checked_enqueue_in(
        Queue(name=ASSET_DEDUP_SCAN_QUEUE, connection=redis),
        timedelta(seconds=max(1.0, min(float(delay_seconds), 300.0))),
        ASSET_DEDUP_SCAN_FUNCTION,
        operation_job_id,
        {
            "scan_id": str(scan_id),
            "_scan_generation": int(successor["generation"]),
        },
        job_id=rq_job_id,
        job_timeout=ASSET_DEDUP_SCAN_SLICE_TIMEOUT_SECONDS,
        result_ttl=7 * 24 * 60 * 60,
        failure_ttl=7 * 24 * 60 * 60,
    )


async def _update_running_scan_operation(
    operation_job_id: str,
    result: dict,
    *,
    successor_rq_job_id: str,
) -> None:
    waiting = result.get("resource_state") == "waiting"
    phase = "waiting" if waiting else "scanning"
    label = (
        "Waiting for resource capacity"
        if waiting
        else f"Scanned {result['assets_scanned']} assets"
    )
    progress = {
        "phase": phase,
        "label": label,
        "current": result["assets_scanned"],
    }
    async with async_session() as db:
        service = TaskService(db)
        task = await service.get(UUID(operation_job_id))
        if task:
            await service.update_task(
                task,
                status="running",
                progress=progress,
                rq_job_id=successor_rq_job_id,
                resource_state=("waiting" if waiting else "yielded"),
                resource_reason=result.get("resource_reason"),
            )
            await db.commit()
    try:
        await asyncio.to_thread(
            set_operation_status,
            operation_job_id,
            "running",
            "asset-dedup-scan",
            progress=progress,
            meta={
                "entity": "assets",
                "scan_id": result["scan_id"],
                "successor_rq_job_id": successor_rq_job_id,
            },
        )
    except Exception:
        logger.warning(
            "Unable to update asset dedup operation status job=%s",
            operation_job_id,
            exc_info=True,
        )


async def _complete_scan_operation(operation_job_id: str, result: dict) -> None:
    progress = {"phase": "complete", "label": "Asset scan complete"}
    try:
        try:
            async with async_session() as db:
                service = TaskService(db)
                task = await service.get(UUID(operation_job_id))
                if task:
                    await service.update_task(
                        task,
                        status="complete",
                        progress=progress,
                        result=result,
                        resource_state="yielded",
                        resource_reason=None,
                    )
                    await db.commit()
        except Exception:
            # AssetDedupScan is the durable business record and is already
            # complete.  A notification write must not roll it back to failed.
            logger.warning(
                "Unable to update completed asset dedup TaskRun job=%s",
                operation_job_id,
                exc_info=True,
            )
        try:
            await asyncio.to_thread(
                set_operation_status,
                operation_job_id,
                "complete",
                "asset-dedup-scan",
                progress=progress,
                result=result,
                meta={"entity": "assets", "scan_id": result["scan_id"]},
            )
        except Exception:
            logger.warning(
                "Unable to publish completed asset dedup operation job=%s",
                operation_job_id,
                exc_info=True,
            )
        # Storage effects have their own durable outbox.  Completion only wakes
        # one bounded drain; it never keeps the scan coordinator alive.
        try:
            await asyncio.to_thread(wake_pending_outboxes, {"dedup": 1})
        except Exception:
            logger.warning(
                "Unable to wake asset dedup storage outbox after scan job=%s",
                operation_job_id,
                exc_info=True,
            )
    finally:
        try:
            await asyncio.to_thread(
                release_owned_operation_lock,
                get_redis(),
                ASSET_DEDUP_SCAN_OPERATION_LOCK,
                operation_job_id,
            )
        except Exception:
            logger.warning(
                "Unable to release completed asset dedup operation lock job=%s",
                operation_job_id,
                exc_info=True,
            )


async def _fail_scan_operation(
    scan_id: UUID,
    operation_job_id: str,
    error: BaseException,
) -> None:
    message = str(error)[:4000] or type(error).__name__
    try:
        scan_complete = False
        try:
            async with async_session() as db:
                scan = await db.get(AssetDedupScan, scan_id)
                scan_complete = bool(scan is not None and scan.status == "complete")
                if scan is not None and not scan_complete:
                    scan.status = "failed"
                    scan.error = message
                service = TaskService(db)
                task = await service.get(UUID(operation_job_id))
                if task is not None and not scan_complete:
                    await service.update_task(
                        task,
                        status="failed",
                        progress={"phase": "failed", "label": "Asset scan failed"},
                        error=message,
                        resource_state="yielded",
                        resource_reason=(
                            "successor_enqueue_failed"
                            if "enqueue" in message.lower()
                            else "scan_slice_failed"
                        ),
                    )
                await db.commit()
        except Exception:
            logger.exception(
                "Unable to persist failed asset dedup scan job=%s",
                operation_job_id,
            )
        if not scan_complete:
            try:
                await asyncio.to_thread(
                    set_operation_status,
                    operation_job_id,
                    "failed",
                    "asset-dedup-scan",
                    progress={"phase": "failed", "label": "Asset scan failed"},
                    error=message,
                    meta={"entity": "assets", "scan_id": str(scan_id)},
                )
            except Exception:
                logger.warning(
                    "Unable to publish failed asset dedup operation job=%s",
                    operation_job_id,
                    exc_info=True,
                )
    finally:
        try:
            await asyncio.to_thread(
                release_owned_operation_lock,
                get_redis(),
                ASSET_DEDUP_SCAN_OPERATION_LOCK,
                operation_job_id,
            )
        except Exception:
            logger.warning(
                "Unable to release failed asset dedup operation lock job=%s",
                operation_job_id,
                exc_info=True,
            )


def run_asset_dedup_scan(job_id: str, options: dict) -> dict:
    """Run one durable scan slice and hand ownership to one delayed successor."""

    scan_id = UUID(options["scan_id"])
    expected_generation = max(0, int(options.get("_scan_generation", 0)))

    async def _run() -> dict:
        try:
            result = await _run_scan(
                scan_id,
                operation_job_id=job_id,
                expected_generation=expected_generation,
            )
            if result["status"] in {"superseded", "failed"}:
                return result
            if result["status"] == "complete":
                await _complete_scan_operation(job_id, result)
                return result

            successor = await _reserve_scan_successor(
                scan_id,
                operation_job_id=job_id,
                expected_generation=expected_generation,
            )
            if successor is None:
                return {**result, "status": "superseded"}
            try:
                await asyncio.to_thread(
                    _enqueue_scan_successor,
                    job_id,
                    scan_id,
                    successor,
                    float(result["successor_delay_seconds"]),
                )
            except Exception as exc:
                # The cursor and generation are already durable.  Surface a
                # terminal, resumable failure rather than leaving a ghost
                # operation with no queued successor.
                raise RuntimeError(
                    f"Unable to enqueue asset dedup scan successor: {exc}"
                ) from exc
            await _update_running_scan_operation(
                job_id,
                result,
                successor_rq_job_id=str(successor["rq_job_id"]),
            )
            return {
                **result,
                "successor_rq_job_id": str(successor["rq_job_id"]),
                "next_generation": int(successor["generation"]),
            }
        except Exception as exc:
            await _fail_scan_operation(scan_id, job_id, exc)
            raise

    return asyncio.run(_run())


def run_asset_dedup_outbox(limit: int = 100) -> dict[str, int | bool | float]:
    bounded = max(1, min(int(limit), 100))
    result = None
    try:
        with measure_stage("asset_dedup_slice", limit=bounded):
            result = asyncio.run(process_asset_dedup_outbox(limit=bounded))
            return result
    finally:
        clear_and_wake_outbox_successor("dedup", result)
