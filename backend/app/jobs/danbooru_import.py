"""RQ jobs for Danbooru one-click imports."""

from __future__ import annotations

import asyncio
import logging

from app.database import async_session
from app.services.danbooru_import import import_all_danbooru_artist
from app.services.operations import set_operation_status

logger = logging.getLogger(__name__)


def run_import_all_danbooru(data: dict, job_id: str) -> dict:
    """Entry point for RQ workers."""
    return asyncio.run(_run_import_all_danbooru(data, job_id))


async def _run_import_all_danbooru(data: dict, job_id: str) -> dict:
    set_operation_status(
        job_id,
        "running",
        "danbooru-import-all",
        progress={"phase": "running", "label": "Importing Danbooru artist"},
        meta={"name": data.get("name") or data.get("tag"), "pixiv_id": data.get("pixiv_id")},
    )
    try:
        async with async_session() as db:
            result = await import_all_danbooru_artist(data, db)
        set_operation_status(
            job_id,
            "complete",
            "danbooru-import-all",
            progress={"phase": "complete", "label": "Import complete"},
            result=result,
            meta={"name": data.get("name") or data.get("tag"), "pixiv_id": data.get("pixiv_id")},
        )
        return result
    except Exception as exc:
        logger.exception("Danbooru import-all failed: job_id=%s", job_id)
        set_operation_status(
            job_id,
            "failed",
            "danbooru-import-all",
            progress={"phase": "failed"},
            error=str(exc),
            meta={"name": data.get("name") or data.get("tag"), "pixiv_id": data.get("pixiv_id")},
        )
        raise
