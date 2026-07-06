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
    from uuid import UUID
    from app.services.tasks import TaskService
    async with async_session() as task_db:
        svc = TaskService(task_db)
        task = await svc.get(UUID(job_id))
        if task:
            await svc.update_task(
                task,
                status="running",
                progress={"phase": "running", "label": f"Clearing {entity}"},
            )
            await task_db.commit()
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
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(
                    task,
                    status="complete",
                    progress={"phase": "complete", "label": result.get("message", "Complete")},
                    result=result,
                )
                await task_db.commit()
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
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(task, status="failed", progress={"phase": "failed"}, error=str(exc))
                await task_db.commit()
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
    from uuid import UUID
    from app.services.tasks import TaskService
    async with async_session() as task_db:
        svc = TaskService(task_db)
        task = await svc.get(UUID(job_id))
        if task:
            await svc.update_task(
                task,
                status="running",
                progress={"phase": "running", "label": "Rebuilding library index..."},
            )
            await task_db.commit()
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
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(
                    task,
                    status="complete",
                    progress={"phase": "complete", "label": result.get("message", "Complete")},
                    result=result,
                )
                await task_db.commit()
        set_operation_status(job_id, "complete", "admin-rebuild",
            progress={"phase": "complete", "label": result.get("message", "Complete")},
            result=result, meta={"entity": "library", **options})
        return result
    except Exception as exc:
        logger.exception("Library rebuild failed: job_id=%s", job_id)
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(task, status="failed", progress={"phase": "failed"}, error=str(exc))
                await task_db.commit()
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


def run_disk_import_operation(job_id: str, options: dict | None = None) -> dict:
    """Entry point for RQ workers — import on-disk download files into the DB."""
    return asyncio.run(_run_disk_import_operation(job_id, options or {}))


async def _run_disk_import_operation(job_id: str, options: dict) -> dict:
    from app.services.disk_import import reconcile_downloads_to_db
    from uuid import UUID
    from app.services.tasks import TaskService
    async with async_session() as task_db:
        svc = TaskService(task_db)
        task = await svc.get(UUID(job_id))
        if task:
            await svc.update_task(
                task,
                status="running",
                progress={"phase": "running", "label": "Scanning download root..."},
            )
            await task_db.commit()
    set_operation_status(job_id, "running", "admin-disk-import",
        progress={"phase": "running", "label": "Scanning download root..."},
        meta={"entity": "disk-import", **options})
    try:
        def update_progress(progress: dict):
            set_operation_status(job_id, "running", "admin-disk-import",
                progress={**progress, "label": f"Imported {progress.get('scanned', 0)} of {progress.get('total', 0)} creators"},
                meta={"entity": "disk-import", **options})

        async with async_session() as db:
            result = await reconcile_downloads_to_db(db, {**options, "parent_task_id": job_id}, update_progress)
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(
                    task,
                    status="complete",
                    progress={"phase": "complete", "label": f"Queued {result['jobs']} import jobs"},
                    result=result,
                )
                await task_db.commit()
        set_operation_status(job_id, "complete", "admin-disk-import",
            progress={"phase": "complete", "label": f"Queued {result['jobs']} import jobs"},
            result=result, meta={"entity": "disk-import", **options})
        return result
    except Exception as exc:
        logger.exception("Disk import failed: job_id=%s", job_id)
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(task, status="failed", progress={"phase": "failed"}, error=str(exc))
                await task_db.commit()
        set_operation_status(job_id, "failed", "admin-disk-import",
            progress={"phase": "failed"}, error=str(exc), meta={"entity": "disk-import", **options})
        raise
    finally:
        from app.services.redis_client import get_redis
        redis = get_redis()
        current = redis.get("library:disk-import:active")
        if isinstance(current, bytes):
            current = current.decode()
        if current == job_id:
            redis.delete("library:disk-import:active")


def run_gitllery_rebuild_operation(job_id: str, options: dict | None = None) -> dict:
    """Entry point for RQ workers — restore manual curation from .gitllery into the DB."""
    return asyncio.run(_run_gitllery_rebuild_operation(job_id, options or {}))


async def _run_gitllery_rebuild_operation(job_id: str, options: dict) -> dict:
    from app.services.gitllery import GitlleryService
    from uuid import UUID
    from app.services.tasks import TaskService
    async with async_session() as task_db:
        svc = TaskService(task_db)
        task = await svc.get(UUID(job_id))
        if task:
            await svc.update_task(
                task,
                status="running",
                progress={"phase": "running", "label": "Restoring curation from disk..."},
            )
            await task_db.commit()
    set_operation_status(job_id, "running", "admin-gitllery-rebuild",
        progress={"phase": "running", "label": "Restoring curation from disk..."},
        meta={"entity": "gitllery-rebuild", **options})
    try:
        async with async_session() as db:
            result = await GitlleryService(db).rebuild_db_from_disk(options.get("repository_id"), dry_run=False)
        label = f"Restored {result['commits_restored']} commits, {result['states_applied']} states"
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(
                    task,
                    status="complete",
                    progress={"phase": "complete", "label": label},
                    result=result,
                )
                await task_db.commit()
        set_operation_status(job_id, "complete", "admin-gitllery-rebuild",
            progress={"phase": "complete", "label": label},
            result=result, meta={"entity": "gitllery-rebuild", **options})
        return result
    except Exception as exc:
        logger.exception("Gitllery rebuild failed: job_id=%s", job_id)
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(task, status="failed", progress={"phase": "failed"}, error=str(exc))
                await task_db.commit()
        set_operation_status(job_id, "failed", "admin-gitllery-rebuild",
            progress={"phase": "failed"}, error=str(exc), meta={"entity": "gitllery-rebuild", **options})
        raise
    finally:
        from app.services.redis_client import get_redis
        redis = get_redis()
        current = redis.get("library:gitllery-rebuild:active")
        if isinstance(current, bytes):
            current = current.decode()
        if current == job_id:
            redis.delete("library:gitllery-rebuild:active")


def run_creator_reenrich_operation(job_id: str, options: dict | None = None) -> dict:
    """Entry point for RQ workers — retry Danbooru mapping for flagged creators."""
    return asyncio.run(_run_creator_reenrich_operation(job_id, options or {}))


async def _run_creator_reenrich_operation(job_id: str, options: dict) -> dict:
    from uuid import UUID
    from app.services.creator_enrichment import reenrich_pending
    from app.services.tasks import TaskService

    async with async_session() as task_db:
        svc = TaskService(task_db)
        task = await svc.get(UUID(job_id))
        if task:
            await svc.update_task(
                task,
                status="running",
                progress={"phase": "running", "label": "Searching Danbooru..."},
            )
            await task_db.commit()
    set_operation_status(job_id, "running", "admin-creator-reenrich",
        progress={"phase": "running", "label": "Searching Danbooru..."},
        meta={"entity": "creator-reenrich", **options})
    try:
        def update_progress(progress: dict):
            set_operation_status(job_id, "running", "admin-creator-reenrich",
                progress={**progress,
                          "label": f"Mapped {progress.get('found', 0)} of {progress.get('scanned', 0)} scanned"},
                meta={"entity": "creator-reenrich", **options})

        async with async_session() as db:
            result = await reenrich_pending(db, progress_cb=update_progress)

        label = f"Mapped {result['found']} creators ({result['not_found']} not on Danbooru)"
        if result.get("aborted"):
            label += " — aborted: Danbooru unreachable"
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(
                    task,
                    status="complete",
                    progress={"phase": "complete", "label": label},
                    result=result,
                )
                await task_db.commit()
        set_operation_status(job_id, "complete", "admin-creator-reenrich",
            progress={"phase": "complete", "label": label},
            result=result, meta={"entity": "creator-reenrich", **options})
        if result.get("found"):
            from app.services.cache import invalidate_api_caches, invalidate_creator_subscription_caches
            invalidate_api_caches("creators")
            invalidate_creator_subscription_caches()
        return result
    except Exception as exc:
        logger.exception("Creator re-enrichment failed: job_id=%s", job_id)
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(task, status="failed", progress={"phase": "failed"}, error=str(exc))
                await task_db.commit()
        set_operation_status(job_id, "failed", "admin-creator-reenrich",
            progress={"phase": "failed"}, error=str(exc), meta={"entity": "creator-reenrich", **options})
        raise
    finally:
        from app.services.redis_client import get_redis
        redis = get_redis()
        current = redis.get("library:creator-reenrich:active")
        if isinstance(current, bytes):
            current = current.decode()
        if current == job_id:
            redis.delete("library:creator-reenrich:active")
