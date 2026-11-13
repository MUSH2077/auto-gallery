"""Application-owned import intent creation and post-commit publication."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from inspect import isawaitable
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models.download_job import DownloadJob
from app.repositories.download_job import DownloadJobRepository
from app.services.import_dispatch import prepare_import_dispatch, publish_prepared_import
from app.services.job_manifest import append_manifest_event
from app.services.job_progress import apply_download_progress, apply_import_progress, publish_progress
from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)
RQ_JOB_TIMEOUT = 7200


def _relative_import_paths(new_json_paths: set[str] | None) -> set[str] | None:
    if new_json_paths is None:
        return None
    download_root = Path(settings.download_root)
    relative_paths: set[str] = set()
    for raw_path in new_json_paths:
        path = Path(raw_path)
        try:
            relative = (
                path.relative_to(download_root)
                if path.is_absolute()
                else path
            )
        except ValueError:
            continue
        relative_paths.add(str(relative))
    return relative_paths


async def prepare_import_intent(
    db,
    download_job_id: UUID,
    *,
    import_error: str | None,
    new_json_paths: set[str] | None,
    locked_artifact_ids: set[UUID] | None = None,
    recoverable_page: bool = False,
    require_assignment: bool = False,
    publisher_checkpoint=None,
):
    """Prepare one child in artifact -> parent -> child -> task lock order."""

    from app.models.storage_artifact import StorageArtifact
    from app.services.artifact_ledger import downloads_artifact_predicate

    relative_paths = _relative_import_paths(new_json_paths)
    now = datetime.now(timezone.utc)
    artifact_conditions = [
        downloads_artifact_predicate(),
        StorageArtifact.download_job_id == download_job_id,
        StorageArtifact.artifact_type == "metadata_json",
        StorageArtifact.import_job_id.is_(None),
    ]
    if recoverable_page:
        artifact_conditions.append(
            (StorageArtifact.state.in_(("new", "failed")))
            | (
                (StorageArtifact.state == "importing")
                & (
                    StorageArtifact.lease_expires_at.is_(None)
                    | (StorageArtifact.lease_expires_at <= now)
                )
            ),
        )
    else:
        artifact_conditions.append(StorageArtifact.state == "new")
    if locked_artifact_ids is not None:
        artifact_conditions.append(StorageArtifact.id.in_(locked_artifact_ids))
    elif relative_paths is not None:
        artifact_conditions.append(StorageArtifact.file_path.in_(relative_paths))

    artifact_rows = []
    if relative_paths is None or relative_paths or locked_artifact_ids:
        artifact_rows = list((await db.execute(
            select(StorageArtifact)
            .where(*artifact_conditions)
            .order_by(
                StorageArtifact.source_work_id,
                StorageArtifact.created_at,
                StorageArtifact.id,
            )
            .with_for_update(of=StorageArtifact)
        )).scalars())
    if require_assignment and not artifact_rows:
        return None

    # All participating artifact rows are locked before the shared owner.
    download_job = (
        await db.execute(
            select(DownloadJob)
            .where(DownloadJob.id == download_job_id)
            .with_for_update(of=DownloadJob)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if download_job is None:
        return None
    if publisher_checkpoint is not None:
        checkpoint = publisher_checkpoint(db, lock_task=True)
        if isawaitable(checkpoint):
            await checkpoint

    repo = DownloadJobRepository(db)
    extra = {"error_log": import_error} if import_error else {}
    import_job = await repo.create_import({
        "download_job_id": download_job_id,
        "status": "enqueued",
        **extra,
    })
    for artifact in artifact_rows:
        artifact.metadata_completion_proof = None
        artifact.state = "new"
        artifact.import_job_id = import_job.id
        artifact.lease_token = None
        artifact.lease_expires_at = None
        artifact.last_error = None
    await db.flush()

    apply_import_progress(
        import_job,
        "enqueued",
        "Queued; waiting for import worker",
        publish=False,
    )
    apply_download_progress(
        download_job,
        "importing",
        "Import job queued; waiting for import worker",
        publish=False,
        import_job_id=str(import_job.id),
    )
    append_manifest_event(
        download_job,
        "import_job_created",
        import_job_id=str(import_job.id),
        reason=import_error,
    )
    from app.services.tasks import TaskService

    task_svc = TaskService(db)
    from app.services.download_lifecycle import apply_download_state
    await apply_download_state(db, download_job, "importing", error=import_error)
    parent_task = await task_svc.get_by_subject("download_job", download_job.id)
    prepared = await prepare_import_dispatch(
        db,
        import_job,
        parent_task_id=parent_task.id,
        job_timeout=RQ_JOB_TIMEOUT,
    )
    return str(import_job.id), prepared.rq_job_id


async def publish_import_intent(
    download_job_id: str,
    import_job_id: str,
    rq_job_id: str,
    new_json_paths: set[str] | None,
) -> str:
    """Publish a committed child intent and its compatibility handoff."""

    # Store new JSON file paths in Redis for the import runner.  The disk copy
    # remains authoritative when Redis is unavailable or publication is delayed.
    try:
        redis_client = get_redis()
    except Exception:
        redis_client = None
        logger.warning(
            "Redis client unavailable while publishing import %s; durable recovery will retry",
            import_job_id,
            exc_info=True,
        )
    if new_json_paths:
        if redis_client is not None:
            try:
                redis_client.setex(
                    f"import:{import_job_id}:files",
                    86400,  # 24h TTL
                    json.dumps(list(new_json_paths)),
                )
            except Exception:
                logger.warning("Failed to store import file list for %s", import_job_id, exc_info=True)
        try:
            fallback_dir = Path(settings.download_root) / ".import-lists"
            fallback_dir.mkdir(parents=True, exist_ok=True)
            fallback_path = fallback_dir / f"{import_job_id}.json"
            with open(fallback_path, "w") as _ff:
                json.dump(list(new_json_paths), _ff)
        except Exception:
            logger.warning("Failed to write import file list fallback for %s", import_job_id, exc_info=True)

    try:
        async with async_session() as publication_db:
            publication = await publish_prepared_import(
                publication_db,
                import_job_id,
                rq_job_id,
                redis_client=redis_client,
            )
    except Exception:
        # The durable pending intent is sufficient for periodic recovery.  This
        # catch also covers an ambiguous failure after RQ accepted the job.
        publication = "deferred"
        logger.warning(
            "Import publication will be recovered job=%s download=%s",
            import_job_id,
            download_job_id,
            exc_info=True,
        )

    waiting_for_redis = publication == "deferred"
    if publication == "invalid":
        download_stage = "failed"
        import_stage = "failed"
        download_message = "Import queue publication failed; operator action required"
        import_message = download_message
    elif waiting_for_redis:
        download_stage = "importing"
        import_stage = "enqueued"
        download_message = "Import job saved; waiting for queue capacity"
        import_message = "Waiting for queue capacity; publication will retry"
    else:
        download_stage = "importing"
        import_stage = "enqueued"
        download_message = "Import job queued; waiting for import worker"
        import_message = "Queued; waiting for import worker"
    try:
        publish_progress(
            download_job_id,
            "download",
            {
                "stage": download_stage,
                "message": download_message,
                "import_job_id": import_job_id,
            },
        )
        publish_progress(
            import_job_id,
            "import",
            {
                "stage": import_stage,
                "message": import_message,
            },
        )
    except Exception:
        logger.warning(
            "Import %s durable publication=%s but progress notification failed",
            import_job_id,
            publication,
            exc_info=True,
        )
    logger.info(
        "Import publication=%s job=%s download=%s",
        publication,
        import_job_id,
        download_job_id,
    )
    return import_job_id


async def enqueue_import(
    download_job_id: str,
    import_error: str | None = None,
    new_json_paths: set[str] | None = None,
    *,
    publisher_checkpoint=None,
):
    """Create and publish one durable, exactly assigned import intent."""

    try:
        async with async_session() as db:
            prepared = await prepare_import_intent(
                db,
                UUID(download_job_id),
                import_error=import_error,
                new_json_paths=new_json_paths,
                publisher_checkpoint=publisher_checkpoint,
            )
            if prepared is None:
                await db.rollback()
                return None
            await db.commit()
    except Exception as exc:
        logger.error(
            "Failed to create import publication for download %s: %s",
            download_job_id,
            exc,
            exc_info=True,
        )
        raise RuntimeError(
            f"Could not create import job for download {download_job_id}"
        ) from exc
    return await publish_import_intent(
        download_job_id,
        *prepared,
        new_json_paths,
    )


async def enqueue_import_from_locked_page(
    db,
    download_job_id: UUID,
    *,
    artifact_ids: set[UUID],
    import_error: str,
) -> str | None:
    """Atomically assign a recovery page before exposing its child intent."""

    from app.models.storage_artifact import StorageArtifact

    prepared = await prepare_import_intent(
        db,
        download_job_id,
        import_error=import_error,
        new_json_paths=None,
        locked_artifact_ids=artifact_ids,
        recoverable_page=True,
        require_assignment=True,
    )
    if prepared is None:
        await db.rollback()
        return None
    assigned_paths = set((await db.execute(
        select(StorageArtifact.file_path).where(
            StorageArtifact.import_job_id == UUID(prepared[0]),
        )
    )).scalars())
    await db.commit()
    return await publish_import_intent(
        str(download_job_id),
        *prepared,
        assigned_paths,
    )
