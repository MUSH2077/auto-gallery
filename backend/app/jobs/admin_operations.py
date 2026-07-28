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
