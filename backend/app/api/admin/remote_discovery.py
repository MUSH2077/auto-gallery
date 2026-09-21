"""Metadata-only, read-only administrator audit of private discovery state."""

from fastapi import Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequireAdminUser
from app.database import get_db
from app.models import RemoteAccount, Subscription, User, UserSubscription
from uuid import UUID

from ._routers import router


@router.get("/audit/remote-accounts")
async def audit_remote_accounts(
    source: str | None = None,
    user_id: int | None = None,
    offset: int = 0,
    limit: int = Query(50, ge=1, le=200),
    admin=RequireAdminUser,
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if source:
        filters.append(RemoteAccount.source == source)
    if user_id is not None:
        filters.append(RemoteAccount.user_id == user_id)
    total = (
        await db.execute(select(func.count(RemoteAccount.id)).where(*filters))
    ).scalar_one()
    rows = (
        await db.execute(
            select(
                RemoteAccount.id,
                RemoteAccount.user_id,
                User.username,
                RemoteAccount.source,
                RemoteAccount.remote_user_id,
                RemoteAccount.remote_username,
                RemoteAccount.auth_method,
                RemoteAccount.is_enabled,
                RemoteAccount.auth_status,
                RemoteAccount.last_authenticated_at,
                RemoteAccount.last_scan_completed_at,
                RemoteAccount.next_scan_at,
                RemoteAccount.auto_import_enabled,
                RemoteAccount.credential_ciphertext.is_not(None).label("has_credentials"),
                RemoteAccount.created_at,
                RemoteAccount.updated_at,
            )
            .join(User, User.id == RemoteAccount.user_id)
            .where(*filters)
            .order_by(RemoteAccount.created_at.desc(), RemoteAccount.id)
            .offset(max(0, offset))
            .limit(limit)
        )
    ).mappings().all()
    return {"total": int(total), "items": [dict(row) for row in rows]}


@router.get("/audit/subscription-members")
async def audit_subscription_members(
    user_id: int | None = None,
    subscription_id: UUID | None = None,
    offset: int = 0,
    limit: int = Query(50, ge=1, le=200),
    admin=RequireAdminUser,
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if user_id is not None:
        filters.append(UserSubscription.user_id == user_id)
    if subscription_id:
        filters.append(UserSubscription.subscription_id == subscription_id)
    total = (
        await db.execute(select(func.count(UserSubscription.id)).where(*filters))
    ).scalar_one()
    rows = (
        await db.execute(
            select(
                UserSubscription.id.label("membership_id"),
                UserSubscription.user_id,
                User.username,
                UserSubscription.subscription_id,
                Subscription.creator_id,
                UserSubscription.name,
                UserSubscription.is_active,
                UserSubscription.sync_enabled,
                UserSubscription.schedule_mode,
                UserSubscription.created_at,
                UserSubscription.updated_at,
            )
            .join(User, User.id == UserSubscription.user_id)
            .join(Subscription, Subscription.id == UserSubscription.subscription_id)
            .where(*filters)
            .order_by(UserSubscription.created_at.desc(), UserSubscription.id)
            .offset(max(0, offset))
            .limit(limit)
        )
    ).mappings().all()
    return {"total": int(total), "items": [dict(row) for row in rows]}
