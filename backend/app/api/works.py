from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from app.auth import RequirePermission
from app.models.user import User
from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.schemas.work import RemoteWorkStateRead, WorkRead, WorkList, WorkListResponse
from app.schemas.asset import PlaybackTicketRead, WorkAssetRead
from app.schemas.curation import BatchCurateRequest, CurationCommitRead
from app.repositories.work import WorkRepository
from app.models.asset import Asset
from app.models.asset_source import AssetSource
from app.models.work import Work
from app.models.work_source import WorkSource
from app.models.tag import Tag
from app.models.work_tag import WorkTag
from app.services.media_assets import is_browser_playable_video, media_kind
from app.services.media_derivatives import (
    media_derivative_status,
    request_media_derivatives,
)
from app.services.media_signing import signed_media_ticket, signed_media_url
from app.services.curation import CurationService
from app.services.search import SearchBackendUnavailable, SearchPermissionError, SearchService
from app.services.search_language import SearchQueryError
from app.services.search_projection_outbox import request_search_projection
from app.remote_discovery.common import RemoteRateLimited, RemoteReauthenticationRequired
from app.services.remote_accounts import (
    RemoteAccountService,
    RemoteCredentialGenerationChanged,
    RemoteWorkStateAccountRequired,
    RemoteWorkStateAccountUnhealthy,
)
from app.services.remote_discovery_rollout import RemoteDiscoveryUnavailable

# Reused as-is (same closure) for both the router-level gate and the
# per-route parameter below — FastAPI's per-request dependency cache keys on
# the callable identity, so declaring it as a route parameter too does not
# trigger a second permission check/DB query; it just captures the User
# already resolved by the router-level dependency.
_require_library = RequirePermission("library")
_require_curation = RequirePermission("curation")

router = APIRouter(dependencies=[_require_library])
# Write/mutation routes on the `work` library entity belong to the
# `curation` module per spec §A2 (favorite/trash/restore/batch-curate/
# batch-tag are curation operations, not library browsing). Included with
# the same "/works" prefix in app/api/__init__.py, so URLs are unchanged.
curation_router = APIRouter(dependencies=[_require_curation])


@router.get("", response_model=WorkListResponse)
async def list_works(
    offset: int = 0, limit: int = 50,
    q: str = "",
    user: User = _require_library,
    db: AsyncSession = Depends(get_db),
):
    permissions = (
        {"library", "curation", "subscriptions", "tasks", "system", "upload"}
        if user.is_admin
        else set(user.permissions or [])
    )
    try:
        result = await SearchService(db).search(
            q,
            offset,
            limit,
            scope="works",
            permissions=permissions,
            force_sfw=not user.nsfw_visible,
        )
    except SearchQueryError as exc:
        raise HTTPException(status_code=422, detail=exc.diagnostic.payload()) from exc
    except SearchPermissionError as exc:
        raise HTTPException(status_code=403, detail={"code": "permission_denied", "message": str(exc)}) from exc
    except SearchBackendUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "search_unavailable", "message": str(exc)}) from exc
    group = result["groups"]["works"]
    return {
        "total": group["total"],
        "items": [WorkList.model_validate(item) for item in group["items"]],
    }


@router.get("/{work_id}", response_model=WorkRead)
async def get_work(work_id: UUID, user: User = _require_library, db: AsyncSession = Depends(get_db)):
    repo = WorkRepository(db)
    work = await repo.get(work_id, force_sfw=not user.nsfw_visible)
    if not work:
        raise HTTPException(status_code=404, detail="Work not found")
    svc = CurationService(db)
    work.curation_state = svc.work_state_payload(await svc.work_state(work_id))
    return work


def _remote_work_state_error(status_code: int, code: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code})


def _sanitized_retry_after(value: object) -> str:
    try:
        return str(max(1, int(value)))
    except (TypeError, ValueError):
        return "1"


_REMOTE_WORK_STATE_ERROR_CONTENT = {
    "content": {
        "application/json": {
            "schema": {
                "allOf": [
                    {"$ref": "#/components/schemas/ApiError"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["detail"],
                        "properties": {
                            "detail": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["code"],
                                "properties": {"code": {"type": "string"}},
                            }
                        },
                    },
                ]
            }
        }
    }
}


_REMOTE_WORK_STATE_RESPONSES = {
    200: {
        "description": "Live Pixiv work state.",
        "headers": {
            "Cache-Control": {
                "description": "Private response that must not be stored.",
                "schema": {"type": "string"},
            }
        },
    },
    409: {
        "description": "Remote work state is unsupported or the account requires attention.",
        **_REMOTE_WORK_STATE_ERROR_CONTENT,
    },
    429: {
        "description": "Remote provider rate limit reached.",
        "headers": {
            "Retry-After": {
                "description": "Positive number of seconds before retrying.",
                "schema": {"type": "string"},
            }
        },
        **_REMOTE_WORK_STATE_ERROR_CONTENT,
    },
    502: {
        "description": "Remote provider is unavailable.",
        **_REMOTE_WORK_STATE_ERROR_CONTENT,
    },
    503: {
        "description": "Remote discovery is unavailable in this deployment.",
        **_REMOTE_WORK_STATE_ERROR_CONTENT,
    },
}


@router.get(
    "/{work_id}/remote-state",
    response_model=RemoteWorkStateRead,
    responses=_REMOTE_WORK_STATE_RESPONSES,
)
async def get_remote_work_state(
    work_id: UUID,
    response: Response,
    user: User = _require_library,
    db: AsyncSession = Depends(get_db),
):
    """Return volatile Pixiv state for a locally visible work without caching it."""

    # Match the normal work detail route before looking up any local source or
    # account. This keeps invisible NSFW works and unknown IDs indistinguishable
    # and prevents an unauthorized request from reaching account/provider state.
    work = await WorkRepository(db).get(work_id, force_sfw=not user.nsfw_visible)
    if not work:
        raise HTTPException(status_code=404, detail="Work not found")

    work_source = (
        await db.execute(
            select(WorkSource)
            .where(WorkSource.work_id == work.id, WorkSource.source == "pixiv")
            .order_by(WorkSource.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if work_source is None:
        raise _remote_work_state_error(409, "remote_work_state_unsupported")

    try:
        state = await RemoteAccountService(db, user.id).fetch_work_state(
            "pixiv", work_source.source_work_id
        )
    except RemoteWorkStateAccountRequired as exc:
        raise _remote_work_state_error(409, "remote_account_required") from exc
    except RemoteWorkStateAccountUnhealthy as exc:
        raise _remote_work_state_error(
            409, "remote_account_reauthentication_required"
        ) from exc
    except RemoteDiscoveryUnavailable as exc:
        raise _remote_work_state_error(503, "remote_discovery_unavailable") from exc
    except RemoteRateLimited as exc:
        raise HTTPException(
            status_code=429,
            detail={"code": "remote_provider_rate_limited"},
            headers={"Retry-After": _sanitized_retry_after(exc.retry_after_seconds)},
        ) from exc
    except RemoteReauthenticationRequired as exc:
        # fetch_work_state marks just the selected account/bindings unhealthy;
        # this read endpoint owns persisting that account-health transition.
        await db.commit()
        raise _remote_work_state_error(
            409, "remote_account_reauthentication_required"
        ) from exc
    except RemoteCredentialGenerationChanged as exc:
        await db.rollback()
        raise _remote_work_state_error(409, "remote_account_stale") from exc
    except Exception as exc:
        # Provider messages may contain remote payloads or credentials. Keep
        # the public vocabulary deliberately opaque.
        raise _remote_work_state_error(502, "remote_provider_unavailable") from exc

    response.headers["Cache-Control"] = "private, no-store"
    return RemoteWorkStateRead(
        source="pixiv",
        source_work_id=state.source_work_id,
        fetched_at=state.fetched_at,
        total_views=state.total_views,
        total_bookmarks=state.total_bookmarks,
        is_bookmarked=state.is_bookmarked,
    )


@curation_router.post("/{work_id}/favorite", response_model=WorkRead)
async def toggle_work_favorite(work_id: UUID, user: User = _require_curation, db: AsyncSession = Depends(get_db)):
    repo = WorkRepository(db)
    work = await repo.get(work_id, force_sfw=not user.nsfw_visible)
    if not work:
        raise HTTPException(status_code=404, detail="Work not found")
    before = bool(work.is_favorite)
    work.is_favorite = not work.is_favorite
    svc = CurationService(db)
    commit = await svc.record_work_favorite(work, before, bool(work.is_favorite))
    commit.stats = {"work_count": 1}
    await request_search_projection(db, [work.id])
    await db.commit()
    await db.refresh(work)
    work.curation_state = svc.work_state_payload(await svc.work_state(work_id))
    return work


@router.get("/{work_id}/sources")
async def get_work_sources(work_id: UUID, user: User = _require_library, db: AsyncSession = Depends(get_db)):
    repo = WorkRepository(db)
    work = await repo.get(work_id, force_sfw=not user.nsfw_visible)
    if not work:
        raise HTTPException(status_code=404, detail="Work not found")
    return await repo.get_sources(work_id)


@router.get("/{work_id}/tags")
async def get_work_tags(work_id: UUID, user: User = _require_library, db: AsyncSession = Depends(get_db)):
    repo = WorkRepository(db)
    work = await repo.get(work_id, force_sfw=not user.nsfw_visible)
    if not work:
        raise HTTPException(status_code=404, detail="Work not found")
    result = await db.execute(
        select(Tag).join(WorkTag, WorkTag.tag_id == Tag.id)
        .where(WorkTag.work_id == work_id)
    )
    tags = result.scalars().all()
    return [{"id": str(t.id), "normalized_name": t.normalized_name, "category": t.category} for t in tags]


@router.get("/{work_id}/assets", response_model=list[WorkAssetRead])
async def get_work_assets(work_id: UUID, user: User = _require_library, db: AsyncSession = Depends(get_db)):
    repo = WorkRepository(db)
    work = await repo.get(work_id, force_sfw=not user.nsfw_visible)
    if not work:
        raise HTTPException(status_code=404, detail="Work not found")
    result = await db.execute(
        select(Asset).join(AssetSource, AssetSource.asset_id == Asset.id)
        .join(WorkSource, WorkSource.id == AssetSource.work_source_id)
        .where(WorkSource.work_id == work_id)
        .distinct()
        .order_by(Asset.created_at)
    )
    assets = result.scalars().all()
    derivative_states = await media_derivative_status(
        db, [asset.id for asset in assets]
    )
    lazy_requests = []
    for asset in assets:
        kind = media_kind(asset.mime_type, asset.file_name)
        if kind in {"image", "animated_image", "video"} and not asset.thumb_sm_path:
            try:
                stat = (Path(settings.download_root) / asset.file_path).stat()
            except OSError:
                continue
            lazy_requests.append(
                {
                    "asset_id": asset.id,
                    "requested": {
                        "thumbnail": True,
                        "dimensions": True,
                        "video": kind == "video",
                    },
                    "source_size": stat.st_size,
                    "source_mtime_ns": stat.st_mtime_ns,
                }
            )
            derivative_states[asset.id] = "pending"
    if lazy_requests:
        await request_media_derivatives(db, lazy_requests)
        await db.commit()
    return [{
        "id": str(a.id), "file_name": a.file_name, "file_path": a.file_path,
        "file_size": a.file_size,
        "width": a.width, "height": a.height, "duration": a.duration, "mime_type": a.mime_type,
        "media_kind": media_kind(a.mime_type, a.file_name),
        "thumb_sm_path": a.thumb_sm_path, "thumb_md_path": a.thumb_md_path,
        "thumb_lg_path": a.thumb_lg_path,
        "thumb_url": f"/media/thumb/{a.id}" if a.thumb_sm_path else None,
        "poster_url": (
            signed_media_url(
                str(a.id),
                "poster",
                settings.media_playback_ttl_seconds,
            )
            if a.thumb_lg_path and media_kind(a.mime_type, a.file_name) == "video"
            else None
        ),
        "preview_url": signed_media_url(str(a.id), "preview"),
        "original_url": signed_media_url(str(a.id), "original"),
        "derivative_status": (
            "ready"
            if derivative_states.get(a.id) in {None, "complete"}
            else derivative_states[a.id]
        ),
        "created_at": a.created_at.isoformat(),
    } for a in assets]


@router.post(
    "/{work_id}/assets/{asset_id}/playback-ticket",
    response_model=PlaybackTicketRead,
)
async def create_playback_ticket(
    work_id: UUID,
    asset_id: UUID,
    user: User = _require_library,
    db: AsyncSession = Depends(get_db),
):
    """Issue a short-lived signed stream URL for one video asset."""
    repo = WorkRepository(db)
    work = await repo.get(work_id, force_sfw=not user.nsfw_visible)
    if not work:
        raise HTTPException(status_code=404, detail="Work not found")
    result = await db.execute(
        select(Asset)
        .join(AssetSource, AssetSource.asset_id == Asset.id)
        .join(WorkSource, WorkSource.id == AssetSource.work_source_id)
        .where(WorkSource.work_id == work_id, Asset.id == asset_id)
        .limit(1)
    )
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found in work")
    if not is_browser_playable_video(asset.mime_type, asset.file_name):
        raise HTTPException(
            status_code=422,
            detail={"code": "not_playable_video", "message": "Playback tickets are only available for video assets"},
        )
    url, expires = signed_media_ticket(
        str(asset.id),
        "stream",
        settings.media_playback_ttl_seconds,
    )
    return PlaybackTicketRead(
        url=url,
        expires_at=datetime.fromtimestamp(expires, tz=timezone.utc),
    )


@curation_router.delete("/{work_id}", status_code=204)
async def delete_work(work_id: UUID, db: AsyncSession = Depends(get_db)):
    """Move a work to trash. Does NOT delete database rows or files from disk."""
    work = await db.get(Work, work_id)
    if not work:
        raise HTTPException(status_code=404, detail="Work not found")
    svc = CurationService(db)
    await svc.trash_works([work_id], message=f"Move work to trash: {work.title or work_id}")


@curation_router.post("/batch-delete")
async def batch_delete_works(data: dict, db: AsyncSession = Depends(get_db)):
    """Move multiple works to trash by ID list."""
    ids = data.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="ids list is required")
    svc = CurationService(db)
    commit = await svc.trash_works([UUID(wid) for wid in ids], message=f"Move {len(ids)} works to trash")
    return {
        "status": "ok",
        "deleted": int((commit.stats or {}).get("work_count", 0)),
        "commit_id": str(commit.id),
        "results": [{"id": wid, "status": "trashed"} for wid in ids],
    }


@curation_router.post("/batch-curate", response_model=CurationCommitRead)
async def batch_curate_works(data: BatchCurateRequest, db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    if data.action == "trash":
        commit = await svc.trash_works(data.ids, reason=data.reason, message=data.message)
    elif data.action == "restore":
        commit = await svc.restore_works(data.ids, reason=data.reason, message=data.message)
    else:
        raise HTTPException(status_code=400, detail="action must be trash or restore")
    return await svc.commit_payload(commit.id)


@curation_router.post("/batch-tag")
async def batch_tag_works(data: dict, db: AsyncSession = Depends(get_db)):
    """Add or remove tags on multiple works."""
    ids = data.get("ids", [])
    action = data.get("action", "add")  # add | remove
    tag_name = data.get("tag", "")
    if not ids or not tag_name:
        raise HTTPException(status_code=400, detail="ids and tag are required")

    from app.models.tag import Tag
    from app.models.work_tag import WorkTag

    # Find or create tag
    normalized = tag_name.strip().lower()
    result = await db.execute(select(Tag).where(Tag.normalized_name == normalized))
    tag = result.scalar_one_or_none()
    if not tag and action == "add":
        tag = Tag(normalized_name=normalized)
        db.add(tag)
        await db.flush()

    updated = 0
    updated_work_ids: list[UUID] = []
    for wid in ids:
        try:
            if action == "add" and tag:
                exists = await db.execute(
                    select(WorkTag).where(WorkTag.work_id == UUID(wid), WorkTag.tag_id == tag.id)
                )
                if not exists.scalar_one_or_none():
                    db.add(WorkTag(work_id=UUID(wid), tag_id=tag.id))
                    updated += 1
                    updated_work_ids.append(UUID(wid))
            elif action == "remove" and tag:
                await db.execute(
                    sql_delete(WorkTag).where(WorkTag.work_id == UUID(wid), WorkTag.tag_id == tag.id)
                )
                updated += 1
                updated_work_ids.append(UUID(wid))
        except Exception:
            import logging
            logging.getLogger(__name__).warning("batch_tag_works failed for %s", wid, exc_info=True)
    if updated_work_ids:
        await request_search_projection(
            db,
            updated_work_ids,
            tag_ids=[tag.id] if tag else (),
        )
    await db.commit()
    return {"status": "ok", "updated": updated}
