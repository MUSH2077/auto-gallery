from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequireAdminUser, RequirePermission
from app.config import settings
from app.database import get_db
from app.schemas.curation import (
    CurationBackfillRunResponse,
    CurationBackfillStatusResponse,
    CurationCommitListResponse,
    CurationCommitRead,
    CurationRevertResponse,
    PurgePreviewRequest,
    PurgePreviewResponse,
    PurgeRequest,
    RuleSuggestionRead,
)
from app.schemas.gitllery import (
    GitlleryCommandRequest,
    GitlleryCommandResponse,
    GitlleryLogResponse,
    GitlleryReconcileResponse,
    GitlleryStatusResponse,
    GitlleryVerifyRequest,
)
from app.services.curation import CurationService
from app.services.gitllery import GitlleryService

router = APIRouter(dependencies=[RequirePermission("curation")])


def _require_gitllery_projection_active() -> None:
    if settings.gitllery_projection_mode.strip().lower() != "active":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "gitllery_shadow_only",
                "message": "Gitllery v1 projection maintenance is disabled while shadow mode is active.",
                "projection_mode": "shadow",
            },
        )


@router.post("/gitllery/push")
@router.post("/gitllery/pull")
async def gitllery_transfer_shadow_only(_admin=RequireAdminUser):
    """Reserve the v1 transfer command surface without unsafe disk mutation.

    The portable transfer protocol is deliberately disabled in this rollout;
    CLI clients receive the same stable shadow-only error as projection
    maintenance instead of a misleading 404 or an implicit full-history walk.
    """

    _require_gitllery_projection_active()
    raise HTTPException(
        status_code=501,
        detail={
            "code": "gitllery_transfer_not_implemented",
            "message": "Gitllery v1 push/pull is not enabled in this release.",
        },
    )


@router.post(
    "/gitllery/commands/preview",
    response_model=GitlleryCommandResponse,
)
async def preview_gitllery_command(
    command: GitlleryCommandRequest,
    user=RequirePermission("curation"),
    db: AsyncSession = Depends(get_db),
):
    """Validate a Gitllery command without committing or touching disk."""

    return await CurationService(db).execute_gitllery_command(
        command,
        actor_id=str(user.id),
        dry_run=True,
    )


@router.post(
    "/gitllery/commands/execute",
    response_model=GitlleryCommandResponse,
)
async def execute_gitllery_command(
    command: GitlleryCommandRequest,
    user=RequirePermission("curation"),
    db: AsyncSession = Depends(get_db),
):
    """Atomically apply one bounded, idempotent Gitllery domain command."""

    return await CurationService(db).execute_gitllery_command(
        command,
        actor_id=str(user.id),
    )


@router.get("/commits", response_model=CurationCommitListResponse)
async def list_curation_commits(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    subject_type: str | None = None,
    subject_id: str | None = None,
    trigger: str | None = None,
    include_baseline: bool = True,
    db: AsyncSession = Depends(get_db),
):
    svc = CurationService(db)
    return await svc.list_commits(
        offset=offset,
        limit=limit,
        subject_type=subject_type,
        subject_id=subject_id,
        trigger=trigger,
        include_baseline=include_baseline,
    )


@router.get("/backfill/status", response_model=CurationBackfillStatusResponse)
async def curation_backfill_status(db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    return await svc.backfill_status()


@router.post("/backfill")
async def run_curation_backfill():
    """Enqueue the baseline backfill — it replays the whole library and must
    run in a worker, never inline in the backend process."""
    from app.services.operations import enqueue_admin_operation
    return await enqueue_admin_operation(
        lock_key="library:curation-backfill:active",
        operation_type="admin-curation-backfill",
        title="Curation baseline backfill",
        entity="curation-backfill",
        func="app.jobs.admin_operations.run_curation_backfill_operation",
        job_timeout=7 * 24 * 60 * 60,
        queue_name="maintenance",
    )


@router.get("/commits/{commit_id}", response_model=CurationCommitRead)
async def get_curation_commit(commit_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    return await svc.commit_payload(commit_id)


@router.post("/commits/{commit_id}/revert", response_model=CurationRevertResponse)
async def revert_curation_commit(commit_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    result = await svc.revert_commit(commit_id)
    return result


@router.post("/purge/preview", response_model=PurgePreviewResponse)
async def preview_purge(data: PurgePreviewRequest, db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    return await svc.purge_preview(data.work_ids)


@router.post("/purge", response_model=CurationCommitRead)
async def purge_trashed_works(data: PurgeRequest, db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    commit = await svc.purge(data.work_ids, message=data.message)
    return await svc.commit_payload(commit.id)


@router.get("/rule-suggestions", response_model=list[RuleSuggestionRead])
async def curation_rule_suggestions(db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    return await svc.rule_suggestions()


@router.get("/gitllery/status", response_model=GitlleryStatusResponse)
async def gitllery_status(deep: bool = Query(False), db: AsyncSession = Depends(get_db)):
    return await GitlleryService(db).status(deep=deep)


@router.get("/repositories/{repository_id}/gitllery/status", response_model=GitlleryStatusResponse)
async def gitllery_repo_status(repository_id: str, deep: bool = Query(False),
                               db: AsyncSession = Depends(get_db)):
    return await GitlleryService(db).status(repository_id, deep=deep)


@router.post("/gitllery/reconcile")
async def gitllery_reconcile(
    repository_id: str | None = None,
    _admin=RequireAdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Wake the bounded v1 projection coordinator.

    The repository hint is informational because one authoritative commit can
    fan out to several portable repositories. No full-history walk runs in the
    request or in one long RQ job.
    """

    _require_gitllery_projection_active()
    from app.services.outbox_coordinator import outbox_counts, wake_pending_outboxes

    counts = await outbox_counts(db)
    wake = wake_pending_outboxes({"gitllery": max(1, counts.get("gitllery", 0))})
    return {
        "status": "enqueued",
        "projection_scope": "library",
        "repository_id": repository_id,
        "ready": counts.get("gitllery", 0),
        **wake,
    }


@router.post("/gitllery/backfill")
@router.post("/gitllery/build")
async def gitllery_backfill(
    _admin=RequireAdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Capture missing historical intents, then wake bounded projection."""

    _require_gitllery_projection_active()
    from sqlalchemy import text
    from app.services.outbox_coordinator import outbox_counts, wake_pending_outboxes

    result = await db.execute(
        text(
            """
            INSERT INTO gitllery_projection_outbox (
              id, created_at, updated_at, commit_id, state, attempts, available_at
            )
            SELECT id, now(), now(), id, 'pending', 0, now()
            FROM curation_commits
            ON CONFLICT (commit_id) DO NOTHING
            """
        )
    )
    await db.commit()
    counts = await outbox_counts(db)
    wake = wake_pending_outboxes({"gitllery": max(1, counts.get("gitllery", 0))})
    return {
        "status": "enqueued",
        "captured": int(result.rowcount or 0),
        "ready": counts.get("gitllery", 0),
        "projection_mode": settings.gitllery_projection_mode,
        **wake,
    }


@router.get("/repositories/{repository_id}/gitllery/log", response_model=GitlleryLogResponse)
async def gitllery_log(repository_id: str, limit: int = Query(50, ge=1, le=200),
                       db: AsyncSession = Depends(get_db)):
    return await GitlleryService(db).log(repository_id, limit)


@router.post("/gitllery/rebuild")
async def gitllery_rebuild(
    dry_run: bool = Query(True),
    repository_id: str | None = None,
    _admin=RequireAdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Compatibility alias for a side-by-side v1 projection build.

    The historical endpoint used to read disk directly into the public
    database. That unsafe behavior is intentionally retired; disaster restore
    has a separate staged workflow.
    """
    if dry_run:
        return {
            "status": "ready",
            "dry_run": True,
            "repository_id": repository_id,
            "projection_mode": settings.gitllery_projection_mode,
            "legacy_layout_will_remain_read_only": True,
        }
    _require_gitllery_projection_active()
    return await gitllery_backfill(_admin, db)


@router.post("/gitllery/verify")
async def gitllery_verify(
    request: GitlleryVerifyRequest,
    _admin=RequireAdminUser,
):
    """Queue bounded verification; never scan repository history in HTTP."""

    from app.services.operations import enqueue_admin_operation

    return await enqueue_admin_operation(
        lock_key=f"gitllery:verify:{request.repository_id}",
        operation_type="admin-gitllery-verify",
        title="Verify Gitllery repository",
        entity="gitllery-verify",
        func="app.jobs.admin_operations.run_gitllery_verify_operation",
        options=request.model_dump(),
        job_timeout=7 * 24 * 60 * 60,
        queue_name="maintenance",
    )
