from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequireAdminUser, RequirePermission
from app.config import settings
from app.database import get_db
from app.schemas.curation import (
    CurationBackfillStatusResponse,
    CurationCommitListResponse,
    CurationCommitRead,
    CurationRevertResponse,
    PurgePreviewRequest,
    PurgePreviewResponse,
    PurgeRequest,
    RuleSuggestionRead,
)
from app.schemas.admin_operations import (
    AdminOperationAccepted,
    AdminOperationSnapshotResponse,
)
from app.schemas.gitllery import (
    GitlleryBuildCreateRequest,
    GitlleryBuildOperationResponse,
    GitlleryBuildRead,
    GitlleryBuildVerifyRequest,
    GitlleryCommandRequest,
    GitlleryCommandResponse,
    GitlleryLogResponse,
    GitlleryReconcileResponse,
    GitlleryStatusResponse,
    GitlleryVerifyRequest,
    GitlleryVerifyOperationResponse,
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


@router.post(
    "/backfill",
    status_code=202,
    response_model=AdminOperationAccepted,
)
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


@router.get(
    "/backfill/latest",
    response_model=AdminOperationSnapshotResponse,
)
async def latest_curation_backfill(db: AsyncSession = Depends(get_db)):
    """Return the durable backfill task so page reloads retain its state."""
    from app.services.operations import latest_successful_admin_operation

    return await latest_successful_admin_operation(
        db,
        operation_type="admin-curation-backfill",
        scope_key="library:curation-backfill:active",
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
async def preview_purge(
    data: PurgePreviewRequest,
    _admin=RequireAdminUser,
    db: AsyncSession = Depends(get_db),
):
    svc = CurationService(db)
    return await svc.purge_preview(data.work_ids)


@router.post("/purge", response_model=CurationCommitRead)
async def purge_trashed_works(
    data: PurgeRequest,
    _admin=RequireAdminUser,
    db: AsyncSession = Depends(get_db),
):
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


async def _enqueue_gitllery_build(
    db: AsyncSession,
    request: GitlleryBuildCreateRequest,
) -> dict:
    from app.services.gitllery.builds import GitlleryBuildService
    from app.services.operations import enqueue_admin_operation

    build = await GitlleryBuildService(db).create(
        scope=request.scope,
        generation=request.generation,
    )
    try:
        operation = await enqueue_admin_operation(
            lock_key="library:gitllery-build:active",
            operation_type="admin-gitllery-build",
            title=f"Build Gitllery {request.scope} generation",
            entity="gitllery-build",
            func="app.jobs.admin_operations.run_gitllery_build_operation",
            options={"build_id": str(build.id)},
            job_timeout=7 * 24 * 60 * 60,
            queue_name="maintenance",
        )
    except Exception as exc:
        build.state = "failed"
        build.last_error = f"Unable to queue Gitllery build: {exc}"[:4000]
        await db.commit()
        raise
    return {
        "build": GitlleryBuildRead.model_validate(build).model_dump(),
        **operation,
    }


@router.post(
    "/gitllery/builds",
    status_code=202,
    response_model=GitlleryBuildOperationResponse,
)
async def create_gitllery_build(
    request: GitlleryBuildCreateRequest,
    _admin=RequireAdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Queue a resumable side-by-side build; active data is never replaced."""

    return await _enqueue_gitllery_build(db, request)


@router.get("/gitllery/builds/{build_id}", response_model=GitlleryBuildRead)
async def get_gitllery_build(
    build_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    from app.services.gitllery.builds import GitlleryBuildService

    return await GitlleryBuildService(db).get(build_id)


@router.post("/gitllery/build", status_code=202)
async def gitllery_build_compatibility(
    _admin=RequireAdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Compatibility entry: start the required canary generation."""

    result = await _enqueue_gitllery_build(
        db,
        GitlleryBuildCreateRequest(scope="canary"),
    )
    return {
        **result,
        "projection_mode": settings.gitllery_projection_mode,
        "captured": 0,
        "ready": 0,
    }


@router.post("/gitllery/backfill", status_code=202)
async def gitllery_backfill(
    _admin=RequireAdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Compatibility entry: start a full build after a passing canary."""

    result = await _enqueue_gitllery_build(
        db,
        GitlleryBuildCreateRequest(scope="full"),
    )
    return {
        **result,
        "projection_mode": settings.gitllery_projection_mode,
        "captured": 0,
        "ready": 0,
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


async def _enqueue_gitllery_verification(
    db: AsyncSession,
    request: GitlleryVerifyRequest,
) -> dict:
    from app.services.gitllery.builds import GitlleryBuildService
    from app.services.operations import enqueue_admin_operation

    verification = await GitlleryBuildService(db).create_verification(
        repository_id=request.repository_id,
        build_id=request.build_id,
        deep=request.deep,
        evidence=request.evidence,
    )

    target = (
        f"build:{request.build_id}"
        if request.build_id
        else request.repository_id or "library"
    )
    try:
        operation = await enqueue_admin_operation(
            lock_key=f"gitllery:verify:{target}",
            operation_type="admin-gitllery-verify",
            title=(
                "Verify Gitllery repository"
                if request.repository_id
                else "Verify Gitllery library"
            ),
            entity="gitllery-verify",
            func="app.jobs.admin_operations.run_gitllery_verify_operation",
            options={
                **request.model_dump(mode="json"),
                "verification_id": str(verification.id),
            },
            job_timeout=7 * 24 * 60 * 60,
            queue_name="maintenance",
        )
    except Exception as exc:
        verification.state = "failed"
        verification.last_error = f"Unable to queue Gitllery verification: {exc}"[
            :4000
        ]
        await db.commit()
        raise
    return {
        "verification": GitlleryBuildRead.model_validate(verification).model_dump(),
        **operation,
    }


@router.post(
    "/gitllery/verify",
    status_code=202,
    response_model=GitlleryVerifyOperationResponse,
)
async def gitllery_verify(
    request: GitlleryVerifyRequest,
    _admin=RequireAdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Queue bounded verification; never scan repository history in HTTP."""

    return await _enqueue_gitllery_verification(db, request)


@router.post(
    "/gitllery/builds/{build_id}/verify",
    status_code=202,
    response_model=GitlleryVerifyOperationResponse,
)
async def verify_gitllery_build(
    build_id: UUID,
    request: GitlleryBuildVerifyRequest,
    _admin=RequireAdminUser,
    db: AsyncSession = Depends(get_db),
):
    return await _enqueue_gitllery_verification(
        db,
        GitlleryVerifyRequest(
            build_id=build_id,
            deep=request.deep,
            evidence=request.evidence,
        ),
    )
