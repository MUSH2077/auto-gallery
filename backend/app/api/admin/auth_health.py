"""Auth health status."""

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

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger(__name__)

from app.auth import RequirePermission
from app.database import async_session, get_db
from app.models.subscription_source import SubscriptionSource

from ._routers import router

@router.get("/auth-status")
async def get_auth_status(db: AsyncSession = Depends(get_db)):
    """List auth health for all subscription sources."""
    from app.models import SubscriptionSource, Subscription, Creator
    from sqlalchemy import select as s

    result = await db.execute(
        s(SubscriptionSource, Subscription, Creator)
        .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
        .join(Creator, Creator.id == Subscription.creator_id)
        .order_by(SubscriptionSource.auth_healthy.asc())
    )
    sources = []
    for ss, sub, creator in result:
        sources.append({
            "id": str(ss.id),
            "source": ss.source,
            "source_url": ss.source_url,
            "source_creator_id": ss.source_creator_id,
            "auth_healthy": ss.auth_healthy,
            "auth_status": ss.auth_status,
            "auth_error_reason": ss.auth_error_reason,
            "last_auth_checked_at": ss.last_auth_checked_at.isoformat() if ss.last_auth_checked_at else None,
            "last_successful_auth": ss.last_successful_auth.isoformat() if ss.last_successful_auth else None,
            "is_enabled": ss.is_enabled,
            "subscription": {
                "id": str(sub.id),
                "name": sub.name,
                "is_active": sub.is_active,
                "sync_enabled": sub.sync_enabled,
            },
            "creator": {
                "id": str(creator.id),
                "name": creator.name,
                "display_name": creator.display_name,
            },
        })
    healthy = sum(1 for s in sources if s["auth_healthy"] is True)
    unhealthy = sum(1 for s in sources if s["auth_healthy"] is False)
    unknown = sum(1 for s in sources if s["auth_healthy"] is None)
    return {
        "sources": sources,
        "summary": {"total": len(sources), "healthy": healthy, "unhealthy": unhealthy, "unknown": unknown},
    }


@router.post("/auth-status/{source_id}/reset")
async def reset_auth_health(source_id: UUID, db: AsyncSession = Depends(get_db)):
    """Reset auth health for a subscription source — allows sync to resume.

    Use this after fixing credentials (e.g. updating refresh token, cookies,
    or password). The next download attempt will re-evaluate auth health.
    """
    from app.models import SubscriptionSource

    ss = await db.get(SubscriptionSource, source_id)
    if not ss:
        raise HTTPException(status_code=404, detail="Subscription source not found")

    ss.auth_healthy = True
    ss.auth_status = None
    ss.auth_error_reason = None
    ss.last_auth_checked_at = None
    from app.services.search_projection_outbox import request_search_projection
    await request_search_projection(db, repository_ids=[ss.id])
    await db.commit()

    return {
        "status": "ok",
        "message": f"Auth health reset for source {source_id}. Next sync will re-evaluate.",
        "source": ss.source,
        "source_url": ss.source_url,
    }


@router.post("/auth-status/reset-all")
async def reset_all_auth_health(db: AsyncSession = Depends(get_db)):
    """Reset auth health for ALL unhealthy subscription sources."""
    from app.models import SubscriptionSource
    from sqlalchemy import update

    result = await db.execute(
        update(SubscriptionSource)
        .where(SubscriptionSource.auth_healthy == False)  # noqa: E712
        .values(auth_healthy=True, auth_status=None, auth_error_reason=None, last_auth_checked_at=None)
        .returning(SubscriptionSource.id)
    )
    reset_ids = [row[0] for row in result.all()]
    from app.services.search_projection_outbox import request_search_projection
    await request_search_projection(db, repository_ids=reset_ids)
    await db.commit()

    return {
        "status": "ok",
        "message": f"Reset auth health for {len(reset_ids)} source(s).",
        "reset_source_ids": [str(source_id) for source_id in reset_ids],
    }


# ── Backup & Restore ──

BACKUP_DIR = Path(settings.download_root) / ".backups"

ALL_BACKUP_CONTENTS = ["database", "gallerydl-config", "app-config", "download-archives", "library-metadata"]

BACKUP_CONTENT_LABELS = {
    "database": "PostgreSQL database dump",
    "gallerydl-config": "gallery-dl config + cookies",
    "app-config": "Application runtime config",
    "download-archives": "Download archive files (per-source SQLite)",
    "library-metadata": "Library metadata.json files",
}
