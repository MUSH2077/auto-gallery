"""Compatibility entry point for obsolete Redis-scheduled backup jobs."""

import asyncio
import logging

from app.services.heavy_io import heavy_io_sync_job

logger = logging.getLogger(__name__)


@heavy_io_sync_job("backup")
def run_auto_backup(interval: int = 24):
    """Route an already queued legacy occurrence through PostgreSQL authority."""
    from app.services.backup_schedule import dispatch_due_backup

    logger.info("Dispatching legacy auto-backup occurrence through TaskRun")
    return asyncio.run(dispatch_due_backup())
