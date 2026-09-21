"""Private persistent scan and discovery candidate workflow."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequirePermission
from app.database import get_db
from app.models import DiscoveryCandidate, RemoteAccount
from app.schemas.remote_discovery import (
    DiscoveryCandidateBatchAction,
    DiscoveryCandidateRead,
    DiscoveryCandidateResolve,
    DiscoveryScanCreate,
    RemoteCreatorDetailRead,
    RemoteWorkImportRead,
    RemoteWorkImportRequest,
    RemoteWorkPageRead,
)
from app.remote_discovery.common import RemoteReauthenticationRequired
from app.services.remote_access_tokens import RemoteAccessTokenError
from app.services.remote_accounts import (
    RemoteCredentialGenerationChanged,
    RemoteWorkStateAccountRequired,
    RemoteWorkStateAccountUnhealthy,
)
from app.services.remote_creator_access import RemoteCreatorAccessService
from app.services.remote_work_import import (
    RemoteSensitiveConfirmationRequired,
    RemoteWorkImportService,
    RemoteWorkSourceBusy,
)
from app.services.backpressure import DownloadAdmissionError
from app.services.remote_discovery import (
    DiscoveryScanInProgress,
    RemoteDiscoveryService,
    prepare_discovery_scan_task,
    publish_discovery_scan,
)
from app.services.remote_discovery_rollout import RemoteDiscoveryUnavailable
from app.services.tasks import TaskService
from app.services.subscription import SubscriptionService
from app.services.tasks import task_payload
from app.schemas.task_actions import TaskRead, TaskPage


router = APIRouter(dependencies=[RequirePermission("subscriptions")])


@router.post("/scans", status_code=201, response_model=TaskRead)
async def create_discovery_scan(
    data: DiscoveryScanCreate,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    try:
        task = await RemoteDiscoveryService(db).create_scan(user.id, data.remote_account_id)
        await prepare_discovery_scan_task(db, task)
        await db.commit()
        await db.refresh(task)
    except DiscoveryScanInProgress as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "scan_in_progress", "task_id": str(exc.task_id)},
        ) from exc
    except (ValueError, RuntimeError) as exc:
        await db.rollback()
        if isinstance(exc, RemoteDiscoveryUnavailable):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "remote_discovery_unavailable",
                    "reason": exc.code,
                    "source": exc.source,
                },
            ) from exc
        status = 404 if "not found" in str(exc).casefold() else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    try:
        publish_discovery_scan(task.id, attempt=task.attempts)
    except Exception as exc:
        current = await db.get(type(task), task.id)
        if current is not None:
            await TaskService(db).update_task(
                current,
                status="failed",
                error=f"Discovery enqueue failed ({type(exc).__name__})",
                reason_code="discovery_enqueue_failed",
            )
            await db.commit()
        raise HTTPException(status_code=503, detail="Discovery queue is unavailable") from exc
    from app.services.task_actions import enrich_actions
    await enrich_actions(db, [task], user=user)
    return task_payload(task)


@router.get("/scans", response_model=TaskPage)
async def list_discovery_scans(
    remote_account_id: UUID | None = None,
    offset: int = 0,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    total, tasks = await RemoteDiscoveryService(db).list_scans(
        user.id,
        account_id=remote_account_id,
        offset=offset,
        limit=limit,
    )
    from app.services.task_actions import enrich_actions
    await enrich_actions(db, tasks, user=user)
    return {"total": total, "items": [task_payload(task) for task in tasks]}


@router.get("/candidates")
async def list_discovery_candidates(
    remote_account_id: UUID | None = None,
    state: str | None = None,
    confidence: str | None = None,
    is_following: bool | None = None,
    local_match: bool | None = None,
    evidence_status: str | None = None,
    offset: int = 0,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    total, candidates = await RemoteDiscoveryService(db).list_candidates(
        user.id,
        account_id=remote_account_id,
        state=state,
        confidence=confidence,
        is_following=is_following,
        local_match=local_match,
        evidence_status=evidence_status,
        offset=offset,
        limit=limit,
    )
    candidate_reads = await RemoteCreatorAccessService(db, user.id).present_candidates(
        list(candidates)
    )
    return {
        "total": total,
        "items": [item.model_dump(mode="json") for item in candidate_reads],
    }


def _remote_detail_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RemoteDiscoveryUnavailable):
        return HTTPException(
            status_code=503,
            detail={
                "code": "remote_discovery_unavailable",
                "reason": exc.code,
                "source": exc.source,
            },
        )
    if isinstance(exc, RemoteAccessTokenError):
        return HTTPException(status_code=400, detail={"code": "invalid_cursor"})
    if isinstance(exc, RemoteWorkStateAccountRequired):
        return HTTPException(status_code=409, detail={"code": "remote_account_required"})
    if isinstance(exc, RemoteWorkStateAccountUnhealthy):
        return HTTPException(status_code=409, detail={"code": "reauthentication_required"})
    if isinstance(exc, RemoteCredentialGenerationChanged):
        return HTTPException(status_code=409, detail={"code": "credential_generation_changed"})
    if isinstance(exc, RemoteReauthenticationRequired):
        return HTTPException(status_code=409, detail={"code": "reauthentication_required"})
    if isinstance(exc, NotImplementedError):
        return HTTPException(status_code=501, detail={"code": "remote_detail_not_supported"})
    if isinstance(exc, ValueError) and "not found" in str(exc).casefold():
        return HTTPException(status_code=404, detail={"code": "candidate_not_found"})
    return HTTPException(status_code=502, detail={"code": "remote_provider_failed"})


@router.get(
    "/candidates/{candidate_id}/remote-detail",
    response_model=RemoteCreatorDetailRead,
)
async def get_candidate_remote_detail(
    candidate_id: UUID,
    response: Response,
    work_type: Literal["illust", "manga"] = Query("illust"),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    try:
        detail = await RemoteCreatorAccessService(db, user.id).get_detail(
            candidate_id,
            work_type=work_type,
            limit=limit,
        )
    except Exception as exc:
        raise _remote_detail_error(exc) from exc
    response.headers["Cache-Control"] = "private, no-store"
    return detail


@router.get(
    "/candidates/{candidate_id}/remote-works",
    response_model=RemoteWorkPageRead,
)
async def get_candidate_remote_works(
    candidate_id: UUID,
    response: Response,
    work_type: Literal["illust", "manga"] = Query("illust"),
    cursor: str | None = Query(None, min_length=40, max_length=10000),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    try:
        works = await RemoteCreatorAccessService(db, user.id).get_works(
            candidate_id,
            work_type=work_type,
            cursor=cursor,
            limit=limit,
        )
    except Exception as exc:
        raise _remote_detail_error(exc) from exc
    response.headers["Cache-Control"] = "private, no-store"
    return works


@router.post(
    "/candidates/{candidate_id}/remote-work-imports",
    response_model=RemoteWorkImportRead,
)
async def import_candidate_remote_work(
    candidate_id: UUID,
    data: RemoteWorkImportRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    try:
        result = await RemoteWorkImportService(db, user.id).import_work(
            candidate_id,
            data.work_token,
            sensitive_content_confirmed=data.sensitive_content_confirmed,
        )
        await db.commit()
    except DownloadAdmissionError as exc:
        await db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.payload()) from exc
    except RemoteWorkSourceBusy as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"code": "source_busy"}) from exc
    except RemoteSensitiveConfirmationRequired as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "sensitive_content_confirmation_required"},
        ) from exc
    except (RemoteAccessTokenError, ValueError) as exc:
        await db.rollback()
        if isinstance(exc, RemoteAccessTokenError):
            raise HTTPException(status_code=400, detail={"code": "invalid_work_token"}) from exc
        message = str(exc).casefold()
        if "not found" in message:
            status, code = 404, "candidate_not_found"
        elif "dismissed" in message:
            status, code = 409, "candidate_dismissed"
        elif "conflict" in message:
            status, code = 409, "candidate_conflict"
        else:
            status, code = 400, "remote_work_import_failed"
        raise HTTPException(status_code=status, detail={"code": code}) from exc
    response.headers["Cache-Control"] = "private, no-store"
    return result


@router.post("/candidates/batch-actions")
async def batch_discovery_candidates(
    data: DiscoveryCandidateBatchAction,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    service = RemoteDiscoveryService(db)
    try:
        if data.action == "import" and data.immediate_sync:
            protected_rows = (
                await db.execute(
                    select(DiscoveryCandidate, RemoteAccount)
                    .join(
                        RemoteAccount,
                        RemoteAccount.id == DiscoveryCandidate.remote_account_id,
                    )
                    .where(
                        DiscoveryCandidate.user_id == user.id,
                        DiscoveryCandidate.id.in_(data.ids),
                        RemoteAccount.user_id == user.id,
                        RemoteAccount.source == "x",
                    )
                )
            ).all()
            if any(
                (candidate.candidate_metadata or {}).get("protected") is True
                and account.download_auth_status != "personal"
                for candidate, account in protected_rows
            ):
                raise ValueError("download_cookie_required")
        candidates = await service.batch_action(user.id, data.ids, action=data.action)
        subscription_ids = {
            candidate.subscription_id
            for candidate in candidates
            if data.action == "import" and candidate.subscription_id is not None
        }
        for candidate in candidates:
            await db.refresh(candidate)
        candidate_payloads = [
            DiscoveryCandidateRead.model_validate(item).model_dump(mode="json")
            for item in candidates
        ]
        await db.commit()
    except (ValueError, RuntimeError) as exc:
        await db.rollback()
        if isinstance(exc, RemoteDiscoveryUnavailable):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "remote_discovery_unavailable",
                    "reason": exc.code,
                    "source": exc.source,
                },
            ) from exc
        status = 404 if "not found" in str(exc).casefold() else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    sync_results = []
    if data.immediate_sync and data.action == "import":
        for subscription_id in subscription_ids:
            sync_results.append(
                await SubscriptionService(db).trigger_sync(subscription_id, user_id=user.id)
            )
    return {
        "items": candidate_payloads,
        "immediate_sync": data.immediate_sync,
        "sync_results": sync_results,
    }


@router.post("/candidates/{candidate_id}/resolve")
async def resolve_discovery_candidate(
    candidate_id: UUID,
    data: DiscoveryCandidateResolve,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    service = RemoteDiscoveryService(db)
    try:
        if data.immediate_sync:
            protected_row = (
                await db.execute(
                    select(DiscoveryCandidate, RemoteAccount)
                    .join(
                        RemoteAccount,
                        RemoteAccount.id == DiscoveryCandidate.remote_account_id,
                    )
                    .where(
                        DiscoveryCandidate.id == candidate_id,
                        DiscoveryCandidate.user_id == user.id,
                        RemoteAccount.user_id == user.id,
                        RemoteAccount.source == "x",
                    )
                )
            ).first()
            if (
                protected_row is not None
                and (protected_row[0].candidate_metadata or {}).get("protected") is True
                and protected_row[1].download_auth_status != "personal"
            ):
                raise ValueError("download_cookie_required")
        await service.resolve_candidate(
            user.id,
            candidate_id,
            creator_id=data.creator_id,
            creator_name=data.creator_name,
        )
        candidate = await service.import_candidate(user.id, candidate_id)
        subscription_id = candidate.subscription_id
        await db.refresh(candidate)
        candidate_payload = DiscoveryCandidateRead.model_validate(candidate).model_dump(mode="json")
        await db.commit()
    except (ValueError, RuntimeError) as exc:
        await db.rollback()
        if isinstance(exc, RemoteDiscoveryUnavailable):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "remote_discovery_unavailable",
                    "reason": exc.code,
                    "source": exc.source,
                },
            ) from exc
        status = 404 if "not found" in str(exc).casefold() else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    sync_result = None
    if data.immediate_sync and subscription_id is not None:
        sync_result = await SubscriptionService(db).trigger_sync(
            subscription_id, user_id=user.id
        )
    return {
        "candidate": candidate_payload,
        "immediate_sync": data.immediate_sync,
        "sync_result": sync_result,
    }
