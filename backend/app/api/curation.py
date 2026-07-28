from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequireAdmin
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
from app.services.curation import CurationService

router = APIRouter(dependencies=[RequireAdmin])


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


@router.post("/backfill", response_model=CurationBackfillRunResponse)
async def run_curation_backfill(db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    return await svc.run_backfill()


@router.get("/commits/{commit_id}", response_model=CurationCommitRead)
async def get_curation_commit(commit_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    return await svc.commit_payload(commit_id)


@router.post("/commits/{commit_id}/revert", response_model=CurationRevertResponse)
async def revert_curation_commit(commit_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    return await svc.revert_commit(commit_id)


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
