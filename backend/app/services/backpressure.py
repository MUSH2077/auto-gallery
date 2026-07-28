"""Capacity gates that stop producers while allowing importers to drain."""

from __future__ import annotations

import shutil

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.storage_artifact import StorageArtifact


async def download_backpressure_reason(db: AsyncSession) -> dict | None:
    usage = shutil.disk_usage(settings.download_root)
    free_gb = usage.free / (1024 ** 3)
    if free_gb < settings.min_download_free_gb:
        return {"code": "disk_backpressure", "free_gb": round(free_gb, 2),
                "minimum_free_gb": settings.min_download_free_gb}
    pending = (await db.execute(
        select(func.count(StorageArtifact.id)).where(
            StorageArtifact.storage_root == "downloads",
            StorageArtifact.artifact_type == "metadata_json",
            StorageArtifact.state.in_(("new", "importing")),
        )
    )).scalar_one()
    if pending >= settings.max_pending_artifacts:
        return {"code": "import_backpressure", "pending": pending,
                "maximum_pending": settings.max_pending_artifacts}
    return None
