"""Delete settled operational history without orphaning workers or domain receipts."""

import asyncio
from sqlalchemy import select, or_
from fastapi import HTTPException
from app.models import DownloadJob, ImportJob, TaskRun, StorageArtifact
from app.services.task_actions import require_job_action


async def delete_history(db, job_id, kind):
    initial = await db.get(DownloadJob if kind == "download" else ImportJob, job_id)
    if initial is None:
        raise HTTPException(404, detail="Job not found")
    parent_id = initial.id if kind == "download" else initial.download_job_id
    # Preserve the lifecycle order used by artifact assignment/finalization:
    # artifacts -> parent -> children -> task rows. Redis publication guards
    # must settle before these TaskRun locks can be obtained.
    await db.execute(select(StorageArtifact.id).where(StorageArtifact.download_job_id == parent_id).order_by(StorageArtifact.id).with_for_update())
    parent = (
        await db.execute(select(DownloadJob).where(DownloadJob.id == parent_id).with_for_update().execution_options(populate_existing=True))
    ).scalar_one_or_none()
    children = list(
        (
            await db.execute(
                select(ImportJob)
                .where(ImportJob.download_job_id == parent_id)
                .order_by(ImportJob.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    job = parent if kind == "download" else next((child for child in children if child.id == job_id), None)
    if job is None:
        raise HTTPException(404, detail="Job not found")
    await require_job_action(db, job, kind, "delete", already_locked=True)
    deleting_ids = {job_id} | ({child.id for child in children} if kind == "download" else set())
    tasks = list(
        (
            await db.execute(
                select(TaskRun)
                .where(or_(TaskRun.subject_type == "download_job", TaskRun.subject_type == "import_job"), TaskRun.subject_id.in_(deleting_ids))
                .order_by(TaskRun.id)
                .with_for_update()
            )
        ).scalars()
    )

    def liveness():
        from app.services.redis_budget import budget_redis
        from app.services.redis_client import get_redis

        with budget_redis(seconds=3, reserve_seconds=0):
            pipeline = get_redis().pipeline(transaction=False)
            for identity in deleting_ids:
                pipeline.exists(f"task:{identity}:heartbeat_ts")
            for task in tasks:
                pipeline.hget(f"rq:job:{task.rq_job_id}", "status")
            values = pipeline.execute()
            return any(values[: len(deleting_ids)]) or any(value in {b"started", "started"} for value in values[len(deleting_ids) :])

    try:
        unsettled = await asyncio.to_thread(liveness)
    except Exception as exc:
        raise HTTPException(409, detail={"code": "invalid_task_action", "action": "delete", "reason": "liveness_unknown"}) from exc
    if unsettled:
        raise HTTPException(409, detail={"code": "invalid_task_action", "action": "delete", "reason": "execution_unsettled"})
    if kind == "download":
        from app.services.operation_attention import upsert_repository_sync_receipt
        from app.services.search_projection_outbox import request_search_projection

        from app.models import RepositorySyncReceipt
        receipt = (await db.execute(select(RepositorySyncReceipt).where(
            RepositorySyncReceipt.source_download_job_id == parent.id).with_for_update())).scalar_one_or_none()
        if receipt is None:
            await upsert_repository_sync_receipt(db, parent)
        elif receipt.status != parent.status:
            raise HTTPException(409, detail={"code": "invalid_task_action", "action": "delete", "reason": "receipt_state_conflict"})
        # A matching receipt is the retained historical outcome. Rebuilding it
        # from compacted operational detail would erase counters/timestamps.
        for child in children:
            await db.delete(child)
        await request_search_projection(db, subscription_ids=[parent.subscription_id] if parent.subscription_id else [])
    for task in tasks:
        await db.delete(task)
    status = job.status
    await db.delete(job)
    await db.commit()
    from app.services.redis_pubsub import TaskEventPublisher

    TaskEventPublisher.publish_status_change(str(job_id), kind, status, "deleted")
