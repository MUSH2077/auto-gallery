"""Bounded, owned batch selection, counted before any action is performed."""

from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import select, func
from app.models import DownloadJob, ImportJob
from app.services.tasks import download_job_visibility_condition, import_job_visibility_condition

BULK_ACTION_LIMIT = 10000


async def owned_batch_ids(db, kind, filters, user_id, *, limit=BULK_ACTION_LIMIT):
    model = DownloadJob if kind == "download" else ImportJob
    visibility = download_job_visibility_condition if kind == "download" else import_job_visibility_condition
    stmt = select(model.id).where(visibility(user_id))
    try:
        if "ids" in filters:
            stmt = stmt.where(model.id.in_([UUID(str(value)) for value in filters["ids"]]))
        if filters.get("status"):
            stmt = stmt.where(model.status == filters["status"])
        if filters.get("statuses"):
            stmt = stmt.where(model.status.in_(filters["statuses"]))
        if kind == "import" and any(filters.get(k) for k in ("source", "subscription_id", "subscription_source_id")):
            stmt = stmt.join(DownloadJob, DownloadJob.id == ImportJob.download_job_id)
        for key in ("source", "subscription_id", "subscription_source_id"):
            if filters.get(key):
                value = filters[key] if key == "source" else UUID(str(filters[key]))
                stmt = stmt.where(getattr(DownloadJob, key) == value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, detail="Invalid batch filters") from exc
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    if total > limit:
        raise HTTPException(409, detail={"code": "batch_limit_exceeded", "total_matched": total, "limit": limit})
    # The count is advisory under concurrency; limit+1 also fences newly matching
    # rows without ever silently accepting an incomplete selection.
    ids = list((await db.execute(stmt.order_by(model.id).limit(limit + 1))).scalars())
    if len(ids) > limit:
        raise HTTPException(409, detail={"code": "batch_limit_exceeded", "total_matched": len(ids), "limit": limit})
    return ids
