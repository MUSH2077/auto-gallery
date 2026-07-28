"""RQ jobs for asset reconciliation scans and durable storage effects."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import or_, select

from app.config import settings
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
    phash_distance,
    structural_similarity,
)
from app.services.operations import set_operation_status
from app.services.tasks import TaskService
from app.services.work_import import WorkImportService


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


async def _finish_event(event_id: UUID) -> None:
    async with async_session() as db:
        event = await db.get(AssetDedupOutbox, event_id)
        if event:
            event.state = "complete"
            event.completed_at = _utcnow()
            event.last_error = None
            await db.commit()


async def _fail_event(event_id: UUID, attempts: int, error: Exception) -> None:
    async with async_session() as db:
        event = await db.get(AssetDedupOutbox, event_id)
        if event:
            event.state = "failed"
            event.last_error = str(error)[:4000]
            event.available_at = _utcnow() + timedelta(
                seconds=min(3600, 30 * (2 ** min(attempts, 7)))
            )
            await db.commit()


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
    distance = phash_distance(asset.phash, representative.phash)
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


async def process_asset_dedup_outbox(*, limit: int = 100) -> dict[str, int]:
    processed = 0
    failed = 0
    while processed + failed < limit:
        event = await _claim_outbox_event()
        if not event:
            break
        try:
            if event["event_type"] == "hardlink":
                await _run_hardlink(event)
            elif event["event_type"] == "quarantine":
                await _run_quarantine(event)
            elif event["event_type"] == "purge":
                await _run_purge(event)
            else:
                raise ValueError(f"Unknown asset dedup event: {event['event_type']}")
        except Exception as exc:
            failed += 1
            await _fail_event(event["id"], event["attempts"], exc)
        else:
            processed += 1
            await _finish_event(event["id"])
    return {"processed": processed, "failed": failed}


async def _run_scan(scan_id: UUID, operation_job_id: str | None = None) -> dict:
    async with async_session() as db:
        scan = await db.get(AssetDedupScan, scan_id)
        if not scan:
            raise KeyError("Asset dedup scan not found")
        scan.status = "running"
        await db.commit()

    try:
        while True:
            async with async_session() as db:
                scan = await db.get(AssetDedupScan, scan_id)
                batch_size = int((scan.options or {}).get("batch_size", 100))
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
                    break
                service = AssetReconciliation(db)
                result = await service.observe(
                    asset_ids,
                    auto_apply=bool((scan.options or {}).get("auto_apply", True)),
                )
                scan.cursor_asset_id = asset_ids[-1]
                scan.assets_scanned += result["assets_scanned"]
                scan.candidates_evaluated += result["candidates_evaluated"]
                scan.cases_created += result["cases_created"]
                scan.assets_grouped += result["assets_grouped"]
                scan.bytes_reclaimable += result["bytes_reclaimable"]
                await db.commit()
                if operation_job_id:
                    set_operation_status(
                        operation_job_id,
                        "running",
                        "asset-dedup-scan",
                        progress={
                            "phase": "scanning",
                            "label": f"Scanned {scan.assets_scanned} assets",
                            "current": scan.assets_scanned,
                        },
                    )
        drain = await process_asset_dedup_outbox(limit=1000)
        if drain["failed"]:
            raise RuntimeError(
                f"{drain['failed']} asset storage action(s) failed; "
                "inspect the dedup outbox before retrying"
            )
    except Exception as exc:
        async with async_session() as db:
            scan = await db.get(AssetDedupScan, scan_id)
            if scan:
                scan.status = "failed"
                scan.error = str(exc)[:4000]
                await db.commit()
        raise

    async with async_session() as db:
        scan = await db.get(AssetDedupScan, scan_id)
        return {
            "scan_id": str(scan.id),
            "status": scan.status,
            "assets_scanned": scan.assets_scanned,
            "candidates_evaluated": scan.candidates_evaluated,
            "cases_created": scan.cases_created,
            "assets_grouped": scan.assets_grouped,
            "bytes_reclaimable": scan.bytes_reclaimable,
        }


def run_asset_dedup_scan(job_id: str, options: dict) -> dict:
    scan_id = UUID(options["scan_id"])

    async def _run() -> dict:
        async with async_session() as db:
            task = await TaskService(db).get(UUID(job_id))
            if task:
                await TaskService(db).update_task(
                    task,
                    status="running",
                    progress={"phase": "scanning", "label": "Scanning image assets"},
                )
                await db.commit()
        try:
            result = await _run_scan(scan_id, operation_job_id=job_id)
        except Exception as exc:
            set_operation_status(
                job_id,
                "failed",
                "asset-dedup-scan",
                error=str(exc),
            )
            async with async_session() as db:
                task = await TaskService(db).get(UUID(job_id))
                if task:
                    await TaskService(db).update_task(
                        task, status="failed", error=str(exc)
                    )
                    await db.commit()
            raise
        set_operation_status(
            job_id,
            "complete",
            "asset-dedup-scan",
            progress={"phase": "complete", "label": "Asset scan complete"},
            result=result,
        )
        async with async_session() as db:
            task = await TaskService(db).get(UUID(job_id))
            if task:
                await TaskService(db).update_task(
                    task,
                    status="complete",
                    progress={"phase": "complete", "label": "Asset scan complete"},
                    result=result,
                )
                await db.commit()
        return result

    return asyncio.run(_run())


def run_asset_dedup_outbox(limit: int = 100) -> dict[str, int]:
    return asyncio.run(process_asset_dedup_outbox(limit=limit))
