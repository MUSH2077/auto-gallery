import logging
from datetime import date, datetime, time, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from app.auth import RequirePermission
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.creator import CreatorCreate, CreatorListResponse, CreatorRead, CreatorUpdate
from app.schemas.curation import CreatorCurationRequest, CurationCommitRead
from app.schemas.source_creator import SourceCreatorCreate, SourceCreatorRead
from app.schemas.creator_link import CreatorLinkCreate, CreatorLinkRead, CreatorLinkUpdate
from app.schemas.deletion import (
    BatchDeletionRequest,
    DeletionPreviewResponse,
    DeletionResultResponse,
)
from app.models.creator import Creator
from app.services.creator import CreatorService
from app.services.cache import (
    cache_get,
    cache_set,
    cache_key,
    TTL,
    invalidate_api_caches,
    invalidate_creator_subscription_caches,
)
from app.services.curation import CurationService
from app.services.search import SearchBackendUnavailable, SearchService
from app.services.search_language import SearchQueryError

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[RequirePermission("library")])
# Write/mutation routes on the `creator` library entity belong to the
# `curation` module per spec §A2 (create/update/delete/batch-delete/merge/
# favorite/curation-ops/creator-link & source CRUD are curation operations,
# not library browsing). Included with the same "/creators" prefix in
# app/api/__init__.py, so URLs are unchanged.
curation_router = APIRouter(dependencies=[RequirePermission("curation")])

@router.get("/count")
async def count_creators(db: AsyncSession = Depends(get_db)):
    """Return total number of creators."""
    ck = cache_key("creators:count")
    cached = cache_get(ck)
    if cached is not None:
        return cached
    result = await db.execute(select(func.count()).select_from(Creator))
    data = {"count": result.scalar() or 0}
    cache_set(ck, data, TTL["creators:count"])
    return data


@router.get("", response_model=CreatorListResponse)
async def list_creators(
    offset: int = 0, limit: int = 50,
    q: str = "",
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await SearchService(db).search(
            q,
            offset,
            limit,
            scope="creators",
            permissions={"library"},
        )
    except SearchQueryError as exc:
        raise HTTPException(status_code=422, detail=exc.diagnostic.payload()) from exc
    except SearchBackendUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "search_unavailable", "message": str(exc)},
        ) from exc
    group = result["groups"]["creators"]
    return {
        "total": group["total"],
        "items": [CreatorRead.model_validate(item) for item in group["items"]],
    }


# ── Batch Operations ──

@curation_router.post("/batch-deletion-preview", response_model=DeletionPreviewResponse)
async def batch_creator_deletion_preview(
    data: BatchDeletionRequest,
    user=RequirePermission("curation"),
    db: AsyncSession = Depends(get_db),
):
    from app.services.hierarchical_deletion import HierarchicalDeletionService

    scope = await HierarchicalDeletionService(db).scope("creator", data.ids)
    return scope.preview_payload(is_admin=user.is_admin)


@curation_router.post(
    "/batch-delete",
    response_model=DeletionResultResponse,
    responses={202: {"model": DeletionResultResponse, "description": "Permanent deletion queued"}},
)
async def batch_delete_creators(
    data: BatchDeletionRequest,
    response: Response,
    user=RequirePermission("curation"),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete creators for curators; enqueue permanent deletion for admins."""
    from app.services.hierarchical_deletion import (
        HierarchicalDeletionService,
        enqueue_permanent_deletion,
    )

    if data.delete_files and not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required to delete files")
    svc = HierarchicalDeletionService(db)
    scope = await svc.scope("creator", data.ids)
    svc.ensure_no_active_jobs(scope)
    if user.is_admin:
        response.status_code = 202
        result = await enqueue_permanent_deletion(
            "creator", data.ids, delete_files=data.delete_files
        )
    else:
        result = await svc.soft_delete(scope)
    invalidate_creator_subscription_caches(include_works=True)
    return result


# ── Dedup & Merge ──

@router.get("/duplicates")
async def list_duplicate_creators(db: AsyncSession = Depends(get_db)):
    """List potential duplicate creators for admin review."""
    from app.services.creator_dedup import find_merge_candidates
    candidates = await find_merge_candidates(db)
    return {"duplicates": candidates, "total": len(candidates)}


@curation_router.post("/merge")
async def merge_creators_endpoint(data: dict, db: AsyncSession = Depends(get_db)):
    """Merge source creators into target. Source creators are deleted after merge.

    Accepts: {target_id: UUID, source_ids: [UUID, ...]}
    """
    from app.services.creator_dedup import merge_creators

    try:
        target_id = UUID(data["target_id"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid target_id")
    source_ids = data.get("source_ids", [])
    results = []
    for source_id_str in source_ids:
        try:
            source_id = UUID(source_id_str)
        except (TypeError, ValueError):
            results.append({
                "source_id": source_id_str,
                "status": "error",
                "error": "invalid_id",
            })
            continue
        try:
            stats = await merge_creators(db, target_id, source_id)
            results.append({"source_id": source_id_str, "status": "merged", **stats})
        except ValueError:
            results.append({
                "source_id": source_id_str,
                "status": "error",
                "error": "merge_rejected",
            })
        except Exception:
            logger.warning(
                "merge_creators failed for source=%s target=%s",
                source_id_str,
                target_id,
                exc_info=True,
            )
            results.append({
                "source_id": source_id_str,
                "status": "error",
                "error": "merge_failed",
            })
    invalidate_creator_subscription_caches(include_works=True)
    return {"status": "ok", "results": results}


@router.get("/{creator_id}", response_model=CreatorRead)
async def get_creator(creator_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    try:
        creator = await svc.get_creator(creator_id)
        curation = CurationService(db)
        creator.curation_state = curation.creator_state_payload(await curation.creator_state(creator_id))
        return creator
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@curation_router.post("", response_model=CreatorRead, status_code=201)
async def create_creator(data: CreatorCreate, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    result = await svc.create_creator(data.model_dump())
    invalidate_api_caches("creators")
    return result


@curation_router.patch("/{creator_id}", response_model=CreatorRead)
async def update_creator(creator_id: UUID, data: CreatorUpdate, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    try:
        result = await svc.update_creator(creator_id, data.model_dump(exclude_none=True))
        invalidate_api_caches("creators")
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))




@router.get("/{creator_id}/timeline")
async def get_creator_timeline(
    creator_id: UUID,
    from_date: date | None = None,
    to_date: date | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Return per-source work counts per day for the creator's activity grid."""
    if from_date is not None and to_date is not None and to_date <= from_date:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_date_range",
                "message": "to_date must be later than from_date",
            },
        )
    range_start = (
        datetime.combine(from_date, time.min, tzinfo=timezone.utc)
        if from_date is not None
        else None
    )
    range_end = (
        datetime.combine(to_date, time.min, tzinfo=timezone.utc)
        if to_date is not None
        else None
    )
    from app.repositories.work import WorkRepository

    repo = WorkRepository(db)
    days, sources = await repo.get_creator_timeline(creator_id, range_start, range_end)
    return {
        "creator_id": str(creator_id),
        "sources": sources,
        "days": days,
        "total": sum(d["total"] for d in days),
    }

@router.get("/{creator_id}/stats")
async def get_creator_stats(creator_id: UUID, db: AsyncSession = Depends(get_db)):
    """Return creator statistics: tag distribution, source breakdown, monthly posting frequency."""
    from app.services.creator import CreatorService
    return await CreatorService(db).get_stats(creator_id)


@router.get("/{creator_id}/subscription-overview")
async def get_creator_subscription_overview(creator_id: UUID, db: AsyncSession = Depends(get_db)):
    """Return the creator's repository overview.

    CreatorService supplies ``can_download`` after ``provider.validate_url``,
    the ``latest_job`` with ``error_log_excerpt``, source
    ``last_attempted_at``/``auth_error_reason``, and
    ``enabled_repository_count``.
    """
    from app.services.creator import CreatorService
    return await CreatorService(db).get_subscription_overview(creator_id)


@curation_router.get("/{creator_id}/deletion-preview", response_model=DeletionPreviewResponse)
async def creator_deletion_preview(
    creator_id: UUID,
    user=RequirePermission("curation"),
    db: AsyncSession = Depends(get_db),
):
    from app.services.hierarchical_deletion import HierarchicalDeletionService

    scope = await HierarchicalDeletionService(db).scope("creator", [creator_id])
    return scope.preview_payload(is_admin=user.is_admin)


@curation_router.delete(
    "/{creator_id}",
    response_model=DeletionResultResponse,
    responses={202: {"model": DeletionResultResponse, "description": "Permanent deletion queued"}},
)
async def delete_creator(
    creator_id: UUID,
    response: Response,
    delete_files: bool = Query(False),
    user=RequirePermission("curation"),
    db: AsyncSession = Depends(get_db),
):
    from app.services.hierarchical_deletion import (
        HierarchicalDeletionService,
        enqueue_permanent_deletion,
    )

    if delete_files and not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required to delete files")
    svc = HierarchicalDeletionService(db)
    scope = await svc.scope("creator", [creator_id])
    svc.ensure_no_active_jobs(scope)
    if user.is_admin:
        response.status_code = 202
        result = await enqueue_permanent_deletion(
            "creator", [creator_id], delete_files=delete_files
        )
    else:
        result = await svc.soft_delete(scope)
    invalidate_creator_subscription_caches(include_works=True)
    return result


@curation_router.post("/{creator_id}/favorite", response_model=CreatorRead)
async def toggle_creator_favorite(creator_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    try:
        result = await svc.toggle_favorite(creator_id)
        invalidate_api_caches("creators")
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@curation_router.post("/{creator_id}/enrich")
async def enrich_creator(creator_id: UUID, db: AsyncSession = Depends(get_db)):
    """Try to establish the Danbooru identity mapping for one creator."""
    from app.services.creator_enrichment import enrich_creator_by_id
    try:
        result = await enrich_creator_by_id(db, creator_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    invalidate_api_caches("creators")
    invalidate_creator_subscription_caches()
    return result


@curation_router.post("/{creator_id}/curation", response_model=CurationCommitRead)
async def curate_creator(creator_id: UUID, data: CreatorCurationRequest, db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    commit = await svc.curate_creator(
        creator_id,
        action=data.action,
        reason=data.reason,
        message=data.message,
    )
    invalidate_api_caches("creators", "works")
    return await svc.commit_payload(commit.id)


@router.get("/{creator_id}/sources", response_model=list[SourceCreatorRead])
async def list_source_creators(creator_id: UUID, db: AsyncSession = Depends(get_db)):
    """List source accounts for a creator."""
    from sqlalchemy import select
    from app.models.source_creator import SourceCreator as SourceCreatorModel
    result = await db.execute(
        select(SourceCreatorModel).where(
            SourceCreatorModel.creator_id == creator_id,
        ).order_by(SourceCreatorModel.source, SourceCreatorModel.source_creator_id)
    )
    return result.scalars().all()


@curation_router.post("/{creator_id}/sources", response_model=SourceCreatorRead, status_code=201)
async def add_source_creator(creator_id: UUID, data: SourceCreatorCreate, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    d = data.model_dump()
    d["creator_id"] = creator_id
    result = await svc.add_source_creator(d)
    invalidate_api_caches("creators", "works")
    return result


@router.get("/{creator_id}/links", response_model=list[CreatorLinkRead])
async def list_creator_links(creator_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    return await svc.list_links(creator_id)


@curation_router.post("/{creator_id}/links", response_model=CreatorLinkRead, status_code=201)
async def add_creator_link(creator_id: UUID, data: CreatorLinkCreate, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    d = data.model_dump()
    d["creator_id"] = creator_id
    return await svc.add_link(d)


@curation_router.patch("/{creator_id}/links/{link_id}", response_model=CreatorLinkRead)
async def update_creator_link(
    creator_id: UUID, link_id: UUID, data: CreatorLinkUpdate, db: AsyncSession = Depends(get_db)
):
    svc = CreatorService(db)
    try:
        return await svc.update_link(link_id, data.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@curation_router.delete("/{creator_id}/links/{link_id}", status_code=204)
async def delete_creator_link(creator_id: UUID, link_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    try:
        await svc.delete_link(link_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
