"""RQ jobs for long-running admin operations."""

from __future__ import annotations

import asyncio
import logging

from app.database import async_session
from app.services.admin_data import clear_entity_data
from app.services.operations import set_operation_status

logger = logging.getLogger(__name__)


def run_clear_operation(entity: str, job_id: str) -> dict:
    """Entry point for RQ workers."""
    return asyncio.run(_run_clear_operation(entity, job_id))


async def _run_clear_operation(entity: str, job_id: str) -> dict:
    set_operation_status(
        job_id,
        "running",
        "admin-clear",
        progress={"phase": "running", "label": f"Clearing {entity}"},
        meta={"entity": entity},
    )
    try:
        async with async_session() as db:
            result = await clear_entity_data(entity, db)
        set_operation_status(
            job_id,
            "complete",
            "admin-clear",
            progress={"phase": "complete", "label": result.get("message", "Complete")},
            result=result,
            meta={"entity": entity},
        )
        return result
    except Exception as exc:
        logger.exception("Admin clear operation failed: job_id=%s entity=%s", job_id, entity)
        set_operation_status(
            job_id,
            "failed",
            "admin-clear",
            progress={"phase": "failed"},
            error=str(exc),
            meta={"entity": entity},
        )
        raise


def run_library_rebuild_operation(job_id: str, options: dict | None = None) -> dict:
    """Entry point for RQ workers — rebuild /library/ from DB."""
    return asyncio.run(_run_library_rebuild_operation(job_id, options or {}))


async def _run_library_rebuild_operation(job_id: str, options: dict) -> dict:
    from app.services.admin_data import rebuild_library_index
    set_operation_status(job_id, "running", "admin-rebuild",
        progress={"phase": "running", "label": "Rebuilding library index..."},
        meta={"entity": "library", **options})
    try:
        def update_progress(progress: dict):
            set_operation_status(job_id, "running", "admin-rebuild",
                progress={**progress, "label": f"Scanned {progress['scanned']} of {progress['total']}"},
                meta={"entity": "library", **options})

        async with async_session() as db:
            result = await rebuild_library_index(db, options, update_progress)
        set_operation_status(job_id, "complete", "admin-rebuild",
            progress={"phase": "complete", "label": result.get("message", "Complete")},
            result=result, meta={"entity": "library", **options})
        return result
    except Exception as exc:
        logger.exception("Library rebuild failed: job_id=%s", job_id)
        set_operation_status(job_id, "failed", "admin-rebuild",
            progress={"phase": "failed"}, error=str(exc), meta={"entity": "library", **options})
        raise
    finally:
        from app.services.redis_client import get_redis
        redis = get_redis()
        current = redis.get("library:rebuild:active")
        if isinstance(current, bytes):
            current = current.decode()
        if current == job_id:
            redis.delete("library:rebuild:active")
