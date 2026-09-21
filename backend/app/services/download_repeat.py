"""Explicit new executions for completed source syncs, with durable request replay."""

from sqlalchemy import select, text
from fastapi import HTTPException
from app.models import DownloadJob, TaskRun, UserSubscription, UserSubscriptionSource, DownloadRepeatIntent
from app.schemas.task_actions import RepeatSyncAccepted
from app.services.tasks import download_job_visibility_condition, deterministic_task_id


def accepted(intent):
    return RepeatSyncAccepted(task_id=intent.task_id, job_id=intent.download_job_id, previous_job_id=intent.previous_job_id, request_id=intent.request_id)


async def repeat_sync(db, user, request_id, *, job_id=None, task_id=None):
    from app.services.task_actions import enrich_actions, require_action
    from app.services.subscription_enqueue import enqueue_subscription_source_sync
    from app.services.redis_budget import budget_redis
    from app.services.download_dispatch import recover_download_dispatch_candidate
    from app.database import async_session

    # Request lock precedes source admission; no TaskRun lock is held across
    # the source/credential transaction or the separate publication session.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"download-repeat:{user.id}:{request_id}"})
    intent = (
        await db.execute(select(DownloadRepeatIntent).where(DownloadRepeatIntent.actor_user_id == user.id, DownloadRepeatIntent.request_id == request_id))
    ).scalar_one_or_none()
    if intent:
        if (job_id is not None and intent.previous_job_id != job_id) or (task_id is not None and intent.previous_task_id != task_id):
            raise HTTPException(409, detail={"code": "request_identity_conflict", "request_id": str(request_id)})
        result = accepted(intent)
        await db.rollback()
        return result
    if task_id is not None:
        from app.services.tasks import TaskService

        task = await TaskService(db).get(task_id)
        if task is None or not await TaskService(db).is_visible_to_user(task, user.id):
            raise HTTPException(404, detail="Task not found")
        if task.subject_type != "download_job":
            raise HTTPException(409, detail={"code": "invalid_task_action", "reason": "requires_completed_source_sync"})
        job_id = task.subject_id
    job = (
        await db.execute(
            select(DownloadJob).where(DownloadJob.id == job_id, download_job_visibility_condition(user.id)).execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(404, detail="DownloadJob not found")
    await enrich_actions(db, [job], user=user, domain_kind="download")
    require_action(job, "repeat_sync")
    membership = (
        await db.execute(
            select(UserSubscription, UserSubscriptionSource)
            .join(UserSubscriptionSource, UserSubscriptionSource.user_subscription_id == UserSubscription.id)
            .where(
                UserSubscription.user_id == user.id,
                UserSubscription.is_active.is_(True),
                UserSubscriptionSource.subscription_source_id == job.subscription_source_id,
                UserSubscriptionSource.is_enabled.is_(True),
            )
            .limit(1)
        )
    ).one_or_none()
    if membership is None:
        raise HTTPException(409, detail={"code": "invalid_task_action", "reason": "membership_unavailable"})
    previous_task = (await db.execute(select(TaskRun.id).where(TaskRun.subject_type == "download_job", TaskRun.subject_id == job.id))).scalar_one_or_none()
    values = dict(
        actor_user_id=user.id, request_id=request_id, previous_job_id=job.id, previous_task_id=previous_task or deterministic_task_id("download_job", job.id)
    )
    with budget_redis(seconds=3, reserve_seconds=0):
        outcome = await enqueue_subscription_source_sync(
            db,
            job.subscription_source_id,
            trigger="repeat_sync",
            triggering_user_subscription_id=membership[0].id,
            triggering_remote_account_id=membership[1].remote_account_id,
            repeat_intent=values,
        )
    if outcome.get("status") != "enqueued":
        await db.rollback()
        raise HTTPException(
            409,
            detail={
                "code": "repeat_sync_not_admitted",
                "reason": outcome.get("skip_reason"),
                "existing_job_id": outcome.get("job_id"),
                "admission": outcome.get("reason"),
            },
        )
    intent = (
        await db.execute(select(DownloadRepeatIntent).where(DownloadRepeatIntent.actor_user_id == user.id, DownloadRepeatIntent.request_id == request_id))
    ).scalar_one()
    result = accepted(intent)
    await db.rollback()
    # Same bounded ordinary durable publisher/recovery path as a batch child.
    # A failed best-effort transport cannot revoke the committed acceptance.
    try:
        with budget_redis(seconds=3, reserve_seconds=0):
            async with async_session() as publication_db:
                child = await publication_db.get(TaskRun, result.task_id)
                current = await publication_db.get(DownloadJob, result.job_id)
                if child and current:
                    await recover_download_dispatch_candidate(publication_db, child, current)
    except Exception:
        import logging

        logging.getLogger(__name__).warning("Repeat dispatch remains pending task=%s", result.task_id, exc_info=True)
    return result
