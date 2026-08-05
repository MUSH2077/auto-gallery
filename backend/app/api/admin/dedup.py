"""Asset-level deduplication review and scan endpoints."""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger(__name__)

from app.auth import RequirePermission
from app.database import async_session, get_db
from app.models import AssetDedupScan
from app.schemas.asset_dedup import (
    AssetDedupCasePage,
    AssetDedupCaseRead,
    AssetDedupDecisionRead,
    AssetDedupDecisionRequest,
    AssetDedupScanRead,
    AssetDedupScanRequest,
)
from app.services.asset_reconciliation import (
    AssetReconciliation,
    DedupIdempotencyConflict,
    StaleDedupCase,
)
from app.services.redis_client import get_redis
from app.services.operations import (
    enqueue_admin_operation,
    get_operation_status,
    set_operation_status,
)

from ._routers import router, curation_ops_router

@curation_ops_router.get("/dedup/cases", response_model=AssetDedupCasePage)
async def list_asset_dedup_cases(
    status: str = Query(default="pending", pattern="^(pending|merged|separate|deferred|all)$"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    return await AssetReconciliation(db).list_cases(
        status=status,
        offset=offset,
        limit=limit,
    )


@curation_ops_router.get(
    "/dedup/cases/{case_id}", response_model=AssetDedupCaseRead
)
async def get_asset_dedup_case(
    case_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    payload = await AssetReconciliation(db).get_case(case_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Asset dedup case not found")
    return payload


@curation_ops_router.post(
    "/dedup/cases/{case_id}/decisions",
    response_model=AssetDedupDecisionRead,
)
async def decide_asset_dedup_case(
    case_id: UUID,
    data: AssetDedupDecisionRequest,
    user=RequirePermission("curation"),
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await AssetReconciliation(db).decide(
            case_id,
            expected_revision=data.expected_revision,
            action=data.action,
            representative_asset_id=data.representative_asset_id,
            actor_type="admin",
            actor_id=str(user.id),
            reason=data.reason,
            idempotency_key=data.idempotency_key,
        )
        await db.commit()
    except KeyError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StaleDedupCase as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DedupIdempotencyConflict as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        from rq import Queue

        Queue(name="operations", connection=get_redis()).enqueue(
            "app.jobs.asset_dedup.run_asset_dedup_outbox",
            100,
            job_timeout=3600,
            result_ttl=86400,
        )
    except Exception:
        logger.warning("Failed to enqueue asset dedup storage drain", exc_info=True)
    return result


@curation_ops_router.post("/dedup/scans")
async def start_asset_dedup_scan(
    data: AssetDedupScanRequest,
    db: AsyncSession = Depends(get_db),
):
    scan = AssetDedupScan(
        status="pending",
        options={
            "auto_apply": data.auto_apply,
            "batch_size": data.batch_size,
        },
    )
    db.add(scan)
    await db.commit()
    await db.refresh(scan)
    try:
        operation = await enqueue_admin_operation(
            lock_key="lock:admin:asset-dedup-scan",
            operation_type="asset-dedup-scan",
            title="Asset dedup scan",
            entity="assets",
            func="app.jobs.asset_dedup.run_asset_dedup_scan",
            options={"scan_id": str(scan.id)},
            job_timeout=14400,
        )
    except Exception:
        scan.status = "failed"
        scan.error = "Unable to enqueue asset dedup scan"
        await db.commit()
        raise
    return {
        "scan_id": str(scan.id),
        "job_id": operation["job_id"],
        "status": operation["status"],
    }


@curation_ops_router.get("/dedup/scans/{scan_id}", response_model=AssetDedupScanRead)
async def get_asset_dedup_scan(
    scan_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    scan = await db.get(AssetDedupScan, scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Asset dedup scan not found")
    return {
        "scan_id": scan.id,
        "status": scan.status,
        "assets_scanned": scan.assets_scanned,
        "candidates_evaluated": scan.candidates_evaluated,
        "cases_created": scan.cases_created,
        "assets_grouped": scan.assets_grouped,
        "bytes_reclaimable": scan.bytes_reclaimable,
        "error": scan.error,
    }


# Compatibility endpoint retained for permission checks and older clients.
@curation_ops_router.get("/dedup/duplicates", deprecated=True)
async def list_duplicates(
    db: AsyncSession = Depends(get_db),
):
    page = await AssetReconciliation(db).list_cases(
        status="pending",
        offset=0,
        limit=100,
    )
    return {
        "duplicates": page["items"],
        "total": page["total"],
        "deprecated": True,
    }


@curation_ops_router.post("/dedup/scan", deprecated=True)
async def scan_duplicates(
    db: AsyncSession = Depends(get_db),
):
    return await start_asset_dedup_scan(
        AssetDedupScanRequest(),
        db,
    )


@curation_ops_router.get("/merge-candidates", deprecated=True)
async def list_merge_candidates(
    db: AsyncSession = Depends(get_db),
):
    page = await AssetReconciliation(db).list_cases(
        status="pending",
        offset=0,
        limit=100,
    )
    return {
        "candidates": page["items"],
        "total": page["total"],
        "deprecated": True,
    }


# ── Auth Status ──
