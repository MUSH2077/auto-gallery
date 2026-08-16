"""RQ jobs for long-running admin operations."""

from __future__ import annotations

import asyncio
import logging

from app.database import async_session
from app.services.admin_data import clear_entity_data
from app.services.operations import release_owned_operation_lock, set_operation_status
from app.services.heavy_io import run_heavy_io_operation

logger = logging.getLogger(__name__)


def run_clear_operation(entity: str, job_id: str) -> dict:
    """Entry point for RQ workers."""
    return asyncio.run(run_heavy_io_operation(
        "operation:clear", job_id, lambda: _run_clear_operation(entity, job_id)))


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
    return asyncio.run(run_heavy_io_operation(
        "operation:library-rebuild", job_id,
        lambda: _run_library_rebuild_operation(job_id, options or {})))


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
        release_owned_operation_lock(redis, "library:rebuild:active", job_id)


def run_disk_import_operation(job_id: str, options: dict | None = None) -> dict:
    """Entry point for RQ workers — import on-disk download files into the DB."""
    return asyncio.run(run_heavy_io_operation(
        "operation:disk-import", job_id,
        lambda: _run_disk_import_operation(job_id, options or {})))


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
        from app.api.admin.settings import invalidate_storage_breakdown_cache
        invalidate_storage_breakdown_cache()
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
        release_owned_operation_lock(redis, "library:disk-import:active", job_id)


def run_gitllery_rebuild_operation(job_id: str, options: dict | None = None) -> dict:
    """Rolling-upgrade safety: never restore disk directly into public DB."""

    raise RuntimeError(
        "legacy Gitllery rebuild is retired; use v1 staged restore"
    )


def run_creator_reenrich_operation(job_id: str, options: dict | None = None) -> dict:
    """Entry point for RQ workers — refresh Danbooru creator mappings."""
    return asyncio.run(run_heavy_io_operation(
        "operation:creator-reenrich", job_id,
        lambda: _run_creator_reenrich_operation(job_id, options or {})))


async def _run_creator_reenrich_operation(job_id: str, options: dict) -> dict:
    from uuid import UUID
    from app.services.creator_enrichment import (
        reenrich_pending,
        refresh_all_creator_mappings,
    )
    from app.services.tasks import TaskService

    refresh_all = options.get("scope") == "all"
    operation_type = (
        "danbooru-mapping-refresh"
        if refresh_all
        else "admin-creator-reenrich"
    )
    entity = "creators" if refresh_all else "creator-reenrich"
    running_label = (
        "Refreshing all Danbooru mappings..."
        if refresh_all
        else "Searching Danbooru..."
    )

    async with async_session() as task_db:
        svc = TaskService(task_db)
        task = await svc.get(UUID(job_id))
        if task:
            await svc.update_task(
                task,
                status="running",
                progress={"phase": "running", "label": running_label},
            )
            await task_db.commit()
    set_operation_status(job_id, "running", operation_type,
        progress={"phase": "running", "label": running_label},
        meta={"entity": entity, **options})
    try:
        def update_progress(progress: dict):
            set_operation_status(job_id, "running", operation_type,
                progress={**progress,
                          "label": f"Mapped {progress.get('found', 0)} of {progress.get('scanned', 0)} scanned"},
                meta={"entity": entity, **options})

        async with async_session() as db:
            if refresh_all:
                result = await refresh_all_creator_mappings(
                    db,
                    progress_cb=update_progress,
                )
            else:
                result = await reenrich_pending(db, progress_cb=update_progress)

        label = f"Mapped {result['found']} creators ({result['not_found']} not on Danbooru)"
        terminal_status = "complete"
        terminal_error = None
        if result.get("aborted"):
            label += " — aborted: Danbooru unreachable"
            terminal_status = "failed"
            terminal_error = "Danbooru became unavailable; partial results were retained"
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(
                    task,
                    status=terminal_status,
                    progress={"phase": terminal_status, "label": label},
                    result=result,
                    error=terminal_error,
                )
                await task_db.commit()
        set_operation_status(job_id, terminal_status, operation_type,
            progress={"phase": terminal_status, "label": label},
            result=result, error=terminal_error,
            meta={"entity": entity, **options})
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
        set_operation_status(job_id, "failed", operation_type,
            progress={"phase": "failed"}, error=str(exc), meta={"entity": entity, **options})
        raise
    finally:
        from app.services.redis_client import get_redis
        redis = get_redis()
        release_owned_operation_lock(
            redis,
            "library:creator-reenrich:active",
            job_id,
        )


def run_gitllery_sync_operation(job_id: str, options: dict | None = None) -> dict:
    """Rolling-upgrade bridge to one bounded v1 segment projection slice."""

    from app.jobs.gitllery_projection import run_gitllery_projection_outbox
    from app.services.redis_client import get_redis

    try:
        return run_gitllery_projection_outbox(limit=25, max_seconds=20.0)
    finally:
        release_owned_operation_lock(
            get_redis(),
            "library:gitllery-sync:active",
            job_id,
        )


def run_gitllery_verify_operation(job_id: str, options: dict | None = None) -> dict:
    return asyncio.run(_run_gitllery_verify_operation(job_id, options or {}))


async def _run_gitllery_verify_operation(job_id: str, options: dict) -> dict:
    from uuid import UUID
    from app.services.gitllery import GitlleryService
    from app.services.tasks import TaskService

    repository_id = str(options.get("repository_id") or "")
    deep = bool(options.get("deep"))
    async with async_session() as db:
        task_service = TaskService(db)
        task = await task_service.get(UUID(job_id))
        if task:
            await task_service.update_task(
                task,
                status="running",
                progress={"phase": "running", "label": "Verifying Gitllery repository"},
            )
            await db.commit()
    set_operation_status(
        job_id,
        "running",
        "admin-gitllery-verify",
        progress={"phase": "running", "label": "Verifying Gitllery repository"},
        meta={"entity": "gitllery-verify", **options},
    )
    try:
        async with async_session() as db:
            result = await GitlleryService(db).verify_segment_repository(
                repository_id,
                deep=deep,
            )
        status = "complete" if result["ok"] else "failed"
        async with async_session() as db:
            task_service = TaskService(db)
            task = await task_service.get(UUID(job_id))
            if task:
                await task_service.update_task(
                    task,
                    status=status,
                    progress={"phase": status, "label": "Gitllery verification finished"},
                    result=result,
                    error=None if result["ok"] else "; ".join(result["errors"]),
                )
                await db.commit()
        set_operation_status(
            job_id,
            status,
            "admin-gitllery-verify",
            progress={"phase": status, "label": "Gitllery verification finished"},
            result=result,
            error=None if result["ok"] else "; ".join(result["errors"]),
            meta={"entity": "gitllery-verify", **options},
        )
        if not result["ok"]:
            raise RuntimeError("Gitllery verification failed")
        return result
    except Exception as exc:
        logger.exception("Gitllery verification failed: job_id=%s", job_id)
        set_operation_status(
            job_id,
            "failed",
            "admin-gitllery-verify",
            progress={"phase": "failed"},
            error=str(exc),
            meta={"entity": "gitllery-verify", **options},
        )
        raise
    finally:
        from app.services.redis_client import get_redis

        release_owned_operation_lock(
            get_redis(),
            f"gitllery:verify:{repository_id}",
            job_id,
        )


async def _run_gitllery_sync_operation(job_id: str, options: dict) -> dict:
    from uuid import UUID
    from app.services.gitllery import GitlleryService
    from app.services.gitllery.service import (
        gitllery_projection_lock,
        rebuild_checkpoint,
    )
    from app.services.tasks import TaskService

    mode = options.get("mode") or "reconcile"
    repository_id = options.get("repository_id")

    async with async_session() as task_db:
        svc = TaskService(task_db)
        task = await svc.get(UUID(job_id))
        if task:
            await svc.update_task(
                task,
                status="running",
                progress={"phase": "running", "label": "Projecting curation history..."},
            )
            await task_db.commit()
    set_operation_status(job_id, "running", "admin-gitllery-sync",
        progress={"phase": "running", "label": "Projecting curation history..."},
        meta={"entity": "gitllery-sync", **options})
    try:
        ordering_lock = gitllery_projection_lock()
        if not ordering_lock.try_acquire():
            raise RuntimeError("Another Gitllery projection coordinator is active")
        try:
            async with async_session() as db:
                svc = GitlleryService(db)
                if mode == "backfill":
                    projected = await svc.backfill(resource_owner=job_id)
                else:
                    projected = await svc.project_pending(
                        repository_id,
                        resource_owner=job_id,
                    )
                # Scoped requests are promoted by project_pending() to one
                # globally ordered pass, because a commit outbox row can span
                # multiple repositories. Re-establish the library checkpoint
                # after either entry point.
                checkpoint_set = await rebuild_checkpoint(
                    db,
                    svc.last_projection_high_water,
                )
        finally:
            ordering_lock.release()

        result = {
            "mode": mode,
            "repository_id": repository_id,
            "projection_scope": "library",
            "projected_repos": len(projected),
            "projected_commits": sum(projected.values()),
            "checkpoint_rebuilt": checkpoint_set,
        }
        label = f"Projected {result['projected_commits']} commits across {result['projected_repos']} repos"
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
        set_operation_status(job_id, "complete", "admin-gitllery-sync",
            progress={"phase": "complete", "label": label},
            result=result, meta={"entity": "gitllery-sync", **options})
        return result
    except Exception as exc:
        logger.exception("Gitllery sync failed: job_id=%s", job_id)
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(task, status="failed", progress={"phase": "failed"}, error=str(exc))
                await task_db.commit()
        set_operation_status(job_id, "failed", "admin-gitllery-sync",
            progress={"phase": "failed"}, error=str(exc), meta={"entity": "gitllery-sync", **options})
        raise
    finally:
        from app.services.redis_client import get_redis
        redis = get_redis()
        release_owned_operation_lock(
            redis,
            "library:gitllery-sync:active",
            job_id,
        )


def run_search_reindex_operation(job_id: str, options: dict | None = None) -> dict:
    """Entry point for RQ workers — full Meilisearch reindex (works/creators/tags)."""
    # SearchService owns bounded search_index slices and takes maintenance only
    # for the atomic swap.  Wrapping the coordinator in the legacy operation
    # lock would otherwise serialize all 67k documents behind one long lease.
    return asyncio.run(_run_search_reindex_operation(job_id, options or {}))


async def _run_search_reindex_operation(job_id: str, options: dict) -> dict:
    from uuid import UUID
    from app.services.search import SearchService
    from app.services.tasks import TaskService

    async with async_session() as task_db:
        svc = TaskService(task_db)
        task = await svc.get(UUID(job_id))
        if task:
            await svc.update_task(
                task,
                status="running",
                progress={"phase": "running", "label": "Rebuilding search index..."},
            )
            await task_db.commit()
    set_operation_status(job_id, "running", "admin-search-reindex",
        progress={"phase": "running", "label": "Rebuilding search index..."},
        meta={"entity": "search-reindex", **options})
    try:
        async with async_session() as db:
            result = await SearchService(db).reindex(resource_owner=job_id)
        label = result.get("message") or "Search reindex complete"
        operation_status = (
            "complete" if result.get("status") == "ok" else "failed"
        )
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(
                    task,
                    status=operation_status,
                    progress={"phase": operation_status, "label": label},
                    result=result,
                )
                await task_db.commit()
        set_operation_status(job_id, operation_status, "admin-search-reindex",
            progress={"phase": operation_status, "label": label},
            result=result, meta={"entity": "search-reindex", **options})
        return result
    except Exception as exc:
        logger.exception("Search reindex failed: job_id=%s", job_id)
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(task, status="failed", progress={"phase": "failed"}, error=str(exc))
                await task_db.commit()
        set_operation_status(job_id, "failed", "admin-search-reindex",
            progress={"phase": "failed"}, error=str(exc), meta={"entity": "search-reindex", **options})
        raise
    finally:
        from app.services.redis_client import get_redis
        redis = get_redis()
        release_owned_operation_lock(
            redis,
            "library:search-reindex:active",
            job_id,
        )


def run_curation_backfill_operation(job_id: str, options: dict | None = None) -> dict:
    """Entry point for RQ workers — baseline curation history backfill."""
    return asyncio.run(_run_curation_backfill_operation(job_id, options or {}))


async def _run_curation_backfill_operation(job_id: str, options: dict) -> dict:
    from uuid import UUID
    from app.services.curation import CurationService
    from app.services.tasks import TaskService

    async with async_session() as task_db:
        svc = TaskService(task_db)
        task = await svc.get(UUID(job_id))
        if task:
            await svc.update_task(
                task,
                status="running",
                progress={"phase": "running", "label": "Backfilling curation baseline..."},
            )
            await task_db.commit()
    set_operation_status(job_id, "running", "admin-curation-backfill",
        progress={"phase": "running", "label": "Backfilling curation baseline..."},
        meta={"entity": "curation-backfill", **options})
    try:
        async with async_session() as db:
            result = await CurationService(db).run_backfill(
                resource_owner=job_id,
            )
        created = result.get("created", {})
        label = (f"Baseline: {created.get('creators', 0)} creators, "
                 f"{created.get('repositories', 0)} repos, {created.get('work_groups', 0)} work groups")
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
        set_operation_status(job_id, "complete", "admin-curation-backfill",
            progress={"phase": "complete", "label": label},
            result=result, meta={"entity": "curation-backfill", **options})
        return result
    except Exception as exc:
        logger.exception("Curation backfill failed: job_id=%s", job_id)
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(task, status="failed", progress={"phase": "failed"}, error=str(exc))
                await task_db.commit()
        set_operation_status(job_id, "failed", "admin-curation-backfill",
            progress={"phase": "failed"}, error=str(exc), meta={"entity": "curation-backfill", **options})
        raise
    finally:
        from app.services.redis_client import get_redis
        redis = get_redis()
        release_owned_operation_lock(
            redis,
            "library:curation-backfill:active",
            job_id,
        )


def run_hierarchy_delete_operation(job_id: str, options: dict | None = None) -> dict:
    """Delete a repository/subscription/creator with bounded curation work."""

    return asyncio.run(run_heavy_io_operation(
        "operation:hierarchy-delete",
        job_id,
        lambda: _run_hierarchy_delete_operation(job_id, options or {}),
    ))


async def _run_hierarchy_delete_operation(job_id: str, options: dict) -> dict:
    from uuid import UUID

    from app.services.hierarchical_deletion import HierarchicalDeletionService
    from app.services.tasks import TaskService

    entity_type = options.get("entity_type")
    if entity_type not in {"repository", "subscription", "creator"}:
        raise ValueError(f"Unsupported hierarchy deletion type: {entity_type}")
    entity_ids = [UUID(value) for value in options.get("entity_ids") or []]
    if not entity_ids:
        raise ValueError("Hierarchy deletion requires at least one target")
    delete_files = bool(options.get("delete_files"))

    async with async_session() as task_db:
        svc = TaskService(task_db)
        task = await svc.get(UUID(job_id))
        if task:
            await svc.update_task(
                task,
                status="running",
                progress={
                    "phase": "preflight",
                    "label": "Checking deletion scope",
                    "current": 0,
                    "total": 0,
                },
            )
            await task_db.commit()
    set_operation_status(
        job_id,
        "running",
        "hierarchy-delete",
        progress={"phase": "preflight", "label": "Checking deletion scope"},
        meta={"entity": "hierarchy-delete", **options},
    )

    async def publish_progress(current: int, total: int, label: str) -> None:
        progress = {
            "phase": "deleting",
            "label": label,
            "current": current,
            "total": total,
        }
        async with async_session() as progress_db:
            progress_service = TaskService(progress_db)
            progress_task = await progress_service.get(UUID(job_id))
            if progress_task:
                await progress_service.update_task(progress_task, progress=progress)
                await progress_db.commit()
        set_operation_status(
            job_id,
            "running",
            "hierarchy-delete",
            progress=progress,
            meta={"entity": "hierarchy-delete", **options},
        )

    try:
        async with async_session() as db:
            deletion = HierarchicalDeletionService(db)
            scope = await deletion.scope(entity_type, entity_ids)
            result = await deletion.permanent_delete(
                scope,
                delete_files=delete_files,
                progress=publish_progress,
            )
        from app.services.cache import invalidate_creator_subscription_caches

        invalidate_creator_subscription_caches(include_works=True)
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(
                    task,
                    status="complete",
                    progress={
                        "phase": "complete",
                        "label": result["message"],
                        "current": result["trashed_or_purged_works"],
                        "total": result["trashed_or_purged_works"],
                    },
                    result=result,
                )
                await task_db.commit()
        set_operation_status(
            job_id,
            "complete",
            "hierarchy-delete",
            progress={"phase": "complete", "label": result["message"]},
            result=result,
            meta={"entity": "hierarchy-delete", **options},
        )
        return result
    except Exception as exc:
        logger.exception("Hierarchy deletion failed: job_id=%s", job_id)
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(
                    task,
                    status="failed",
                    progress={"phase": "failed", "label": "Deletion failed"},
                    error=str(exc),
                )
                await task_db.commit()
        set_operation_status(
            job_id,
            "failed",
            "hierarchy-delete",
            progress={"phase": "failed", "label": "Deletion failed"},
            error=str(exc),
            meta={"entity": "hierarchy-delete", **options},
        )
        raise
    finally:
        from app.services.redis_client import get_redis

        release_owned_operation_lock(
            get_redis(),
            "library:hierarchy-delete:active",
            job_id,
        )
