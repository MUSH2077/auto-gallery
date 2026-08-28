"""Private user overlay for canonical subscriptions and repositories.

Canonical rows are deliberately retained as shared compatibility caches.  This
module is the only Task 3 write path that derives those caches from member
policy; later scheduler work extends the same helper instead of adding another
aggregate implementation.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DiscoveryCandidate,
    RemoteAccount,
    Subscription,
    SubscriptionSource,
    UserSubscription,
    UserSubscriptionSource,
)
from app.models.creator import Creator
from app.schemas.subscription import SubscriptionRead
from app.schemas.subscription_source import SubscriptionSourceRead
from app.providers import registry as provider_registry


_MEMBERSHIP_FIELDS = {
    "name",
    "is_active",
    "sync_enabled",
    "sync_interval_hours",
    "schedule_mode",
    "schedule_rule",
    "scheduled_times",
}


async def recompute_subscription_membership_cache(
    db: AsyncSession,
    subscription_id: UUID,
) -> None:
    """Lock and recompute the canonical compatibility cache transactionally."""

    subscription = (
        await db.execute(
            select(Subscription)
            .where(Subscription.id == subscription_id)
            .with_for_update(of=Subscription)
        )
    ).scalar_one_or_none()
    if subscription is None:
        return

    active_members = (
        await db.execute(
            select(UserSubscription).where(
                UserSubscription.subscription_id == subscription_id,
                UserSubscription.is_active.is_(True),
            )
        )
    ).scalars().all()
    subscription.is_active = bool(active_members)
    subscription.sync_enabled = any(member.sync_enabled for member in active_members)
    if subscription.sync_enabled:
        subscription.sync_interval_hours = min(
            member.sync_interval_hours for member in active_members if member.sync_enabled
        )

    sources = (
        await db.execute(
            select(SubscriptionSource)
            .where(SubscriptionSource.subscription_id == subscription_id)
            .with_for_update(of=SubscriptionSource)
        )
    ).scalars().all()
    active_ids = {member.id for member in active_members if member.sync_enabled}
    for source in sources:
        bindings = (
            await db.execute(
                select(UserSubscriptionSource).where(
                    UserSubscriptionSource.subscription_source_id == source.id,
                    UserSubscriptionSource.user_subscription_id.in_(active_ids)
                    if active_ids
                    else UserSubscriptionSource.id.is_(None),
                    UserSubscriptionSource.is_enabled.is_(True),
                )
            )
        ).scalars().all()
        source.is_enabled = bool(bindings)
        due = [binding.next_sync_at for binding in bindings if binding.next_sync_at is not None]
        source.next_sync_at = min(due) if due else None
        source.auth_healthy = any(binding.auth_healthy for binding in bindings) if bindings else True
    await db.flush()


class SubscriptionMembershipService:
    """Operate on one user's membership without mutating private policy globally."""

    def __init__(self, db: AsyncSession, user_id: int):
        self.db = db
        self.user_id = user_id

    async def _membership(self, subscription_id: UUID, *, lock: bool = False) -> UserSubscription | None:
        stmt = select(UserSubscription).where(
            UserSubscription.user_id == self.user_id,
            UserSubscription.subscription_id == subscription_id,
        )
        if lock:
            stmt = stmt.with_for_update(of=UserSubscription)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def require_membership(self, subscription_id: UUID, *, lock: bool = False) -> UserSubscription:
        member = await self._membership(subscription_id, lock=lock)
        if member is None:
            raise ValueError("Subscription not found")
        return member

    async def ensure_membership(
        self,
        subscription: Subscription,
        **overrides: Any,
    ) -> UserSubscription:
        existing = await self._membership(subscription.id, lock=True)
        if existing is not None:
            return existing
        schedule_mode = overrides.get("schedule_mode", subscription.schedule_mode)
        sync_enabled = overrides.get("sync_enabled", subscription.sync_enabled)
        if schedule_mode == "manual" or sync_enabled is False:
            schedule_mode = "manual"
            sync_enabled = False
        member = UserSubscription(
            user_id=self.user_id,
            subscription_id=subscription.id,
            name=overrides.get("name", subscription.name),
            is_active=overrides.get("is_active", subscription.is_active),
            sync_enabled=sync_enabled,
            sync_interval_hours=overrides.get(
                "sync_interval_hours", max(1, subscription.sync_interval_hours or 6)
            ),
            schedule_mode=schedule_mode,
            schedule_rule=overrides.get("schedule_rule", subscription.schedule_rule),
            scheduled_times=overrides.get("scheduled_times", subscription.scheduled_times),
        )
        self.db.add(member)
        await self.db.flush()
        return member

    async def create_or_join(self, data: dict[str, Any]) -> SubscriptionRead:
        """Reuse a canonical creator subscription and add only this user's row."""

        creator_id = data.get("creator_id")
        creator = await self.db.get(Creator, creator_id)
        if creator is None:
            raise ValueError("Creator not found")
        subscription = (
            await self.db.execute(
                select(Subscription).where(Subscription.creator_id == creator_id).limit(1)
            )
        ).scalar_one_or_none()
        if subscription is None:
            mode = data.get("schedule_mode")
            sync_enabled = data.get("sync_enabled", True)
            if mode == "inherit":
                mode = None
            if mode == "manual" or sync_enabled is False:
                mode = "manual"
                sync_enabled = False
            subscription = Subscription(
                creator_id=creator_id,
                name=data.get("name"),
                is_active=data.get("is_active", True),
                sync_enabled=sync_enabled,
                sync_interval_hours=data.get("sync_interval_hours", 6),
                schedule_mode=mode,
                schedule_rule=data.get("schedule_rule") if mode == "calendar" else None,
                scheduled_times=data.get("scheduled_times"),
            )
            self.db.add(subscription)
            await self.db.flush()
        member = await self.ensure_membership(subscription, **data)
        canonical_sources = (
            await self.db.execute(
                select(SubscriptionSource).where(
                    SubscriptionSource.subscription_id == subscription.id
                )
            )
        ).scalars().all()
        for source in canonical_sources:
            await self.ensure_source_binding(member, source)
        await recompute_subscription_membership_cache(self.db, subscription.id)
        await self.db.flush()
        return await self.get(subscription.id)

    async def add_or_bind_source(
        self,
        subscription_id: UUID,
        data: dict[str, Any],
    ) -> SubscriptionSourceRead:
        member = await self.require_membership(subscription_id)
        source_name = str(data.get("source") or "")
        try:
            provider = provider_registry.get(source_name)
        except KeyError as exc:
            raise ValueError(f"Unknown source provider: {source_name}") from exc
        source_url = data.get("source_url")
        if source_url:
            source_url = provider.normalize_url(source_url) or source_url
            if not provider.validate_url(source_url):
                raise ValueError(f"Invalid URL for source '{source_name}': {data['source_url']}")
        source_creator_id = data.get("source_creator_id")
        predicates = [
            SubscriptionSource.subscription_id == subscription_id,
            SubscriptionSource.source == source_name,
        ]
        identities = []
        if source_url:
            identities.append(SubscriptionSource.source_url == source_url)
        if source_creator_id:
            identities.append(SubscriptionSource.source_creator_id == source_creator_id)
        source = None
        if identities:
            from sqlalchemy import or_

            source = (
                await self.db.execute(
                    select(SubscriptionSource).where(*predicates, or_(*identities)).limit(1)
                )
            ).scalar_one_or_none()
        if source is None:
            source = SubscriptionSource(
                subscription_id=subscription_id,
                source=source_name,
                source_creator_id=source_creator_id,
                source_url=source_url,
                is_enabled=bool(data.get("is_enabled", True)),
            )
            self.db.add(source)
            await self.db.flush()
        await self.ensure_source_binding(
            member,
            source,
            is_enabled=bool(data.get("is_enabled", True)),
        )
        await recompute_subscription_membership_cache(self.db, subscription_id)
        await self.db.flush()
        return next(item for item in await self.list_sources(subscription_id) if item.id == source.id)

    async def ensure_source_binding(
        self,
        membership: UserSubscription,
        source: SubscriptionSource,
        *,
        remote_account_id: UUID | None = None,
        is_enabled: bool | None = None,
    ) -> UserSubscriptionSource:
        if remote_account_id is not None:
            account = (
                await self.db.execute(
                    select(RemoteAccount).where(
                        RemoteAccount.id == remote_account_id,
                        RemoteAccount.user_id == self.user_id,
                        RemoteAccount.source == source.source,
                    )
                )
            ).scalar_one_or_none()
            if account is None:
                raise ValueError("Remote account not found for this subscription source")
        binding = (
            await self.db.execute(
                select(UserSubscriptionSource).where(
                    UserSubscriptionSource.user_subscription_id == membership.id,
                    UserSubscriptionSource.subscription_source_id == source.id,
                )
            )
        ).scalar_one_or_none()
        if binding is not None:
            if remote_account_id is not None:
                binding.remote_account_id = remote_account_id
            if is_enabled is not None:
                binding.is_enabled = is_enabled
            await self.db.flush()
            return binding
        binding = UserSubscriptionSource(
            user_id=self.user_id,
            subscription_id=membership.subscription_id,
            user_subscription_id=membership.id,
            subscription_source_id=source.id,
            remote_account_id=remote_account_id,
            is_enabled=source.is_enabled if is_enabled is None else is_enabled,
            auth_healthy=True,
            last_successful_auth=source.last_successful_auth,
            last_synced_at=source.last_synced_at,
            last_attempted_at=source.last_attempted_at,
            next_sync_at=source.next_sync_at,
            auth_status=source.auth_status,
            auth_error_reason=source.auth_error_reason,
            last_auth_checked_at=source.last_auth_checked_at,
        )
        self.db.add(binding)
        await self.db.flush()
        return binding

    async def _subscription_view(
        self,
        subscription: Subscription,
        membership: UserSubscription,
    ) -> SubscriptionRead:
        creator = await self.db.get(Creator, subscription.creator_id)
        provenance = (
            await self.db.execute(
                select(DiscoveryCandidate, RemoteAccount.source)
                .join(RemoteAccount, RemoteAccount.id == DiscoveryCandidate.remote_account_id)
                .where(DiscoveryCandidate.user_subscription_id == membership.id)
                .order_by(DiscoveryCandidate.imported_at.asc().nullslast(), DiscoveryCandidate.id.asc())
                .limit(1)
            )
        ).first()
        source_count = (
            await self.db.execute(
                select(func.count(UserSubscriptionSource.id)).where(
                    UserSubscriptionSource.user_subscription_id == membership.id
                )
            )
        ).scalar_one()
        enabled_source_count = (
            await self.db.execute(
                select(func.count(UserSubscriptionSource.id)).where(
                    UserSubscriptionSource.user_subscription_id == membership.id,
                    UserSubscriptionSource.is_enabled.is_(True),
                )
            )
        ).scalar_one()
        return SubscriptionRead.model_validate(
            {
                "id": subscription.id,
                "membership_id": membership.id,
                "creator_id": subscription.creator_id,
                "creator_name": (creator.display_name or creator.name) if creator else None,
                "creator_display_name": creator.display_name if creator else None,
                "name": membership.name,
                "is_active": membership.is_active,
                "sync_enabled": membership.sync_enabled,
                "sync_interval_hours": membership.sync_interval_hours,
                "schedule_mode": membership.schedule_mode,
                "schedule_rule": membership.schedule_rule,
                "scheduled_times": membership.scheduled_times,
                "last_synced_at": subscription.last_synced_at,
                "source_count": int(source_count),
                "enabled_source_count": int(enabled_source_count),
                "created_at": subscription.created_at,
                "updated_at": membership.updated_at,
                "discovery_candidate_id": provenance[0].id if provenance else None,
                "discovered_via": provenance[1] if provenance else None,
            }
        )

    async def get(self, subscription_id: UUID) -> SubscriptionRead:
        membership = await self.require_membership(subscription_id)
        subscription = await self.db.get(Subscription, subscription_id)
        if subscription is None:  # defensive; FK normally guarantees this
            raise ValueError("Subscription not found")
        return await self._subscription_view(subscription, membership)

    async def list(self, *, offset: int = 0, limit: int = 50) -> list[SubscriptionRead]:
        rows = (
            await self.db.execute(
                select(Subscription, UserSubscription)
                .join(UserSubscription, UserSubscription.subscription_id == Subscription.id)
                .where(UserSubscription.user_id == self.user_id)
                .order_by(UserSubscription.created_at.desc(), UserSubscription.id)
                .offset(max(0, offset))
                .limit(max(1, min(limit, 200)))
            )
        ).all()
        return [await self._subscription_view(subscription, member) for subscription, member in rows]

    async def count(self) -> int:
        return int(
            (
                await self.db.execute(
                    select(func.count(UserSubscription.id)).where(UserSubscription.user_id == self.user_id)
                )
            ).scalar_one()
        )

    async def subscription_ids(self) -> set[UUID]:
        return set(
            (
                await self.db.execute(
                    select(UserSubscription.subscription_id).where(
                        UserSubscription.user_id == self.user_id
                    )
                )
            ).scalars()
        )

    async def update(self, subscription_id: UUID, data: dict[str, Any]) -> SubscriptionRead:
        member = await self.require_membership(subscription_id, lock=True)
        values = {key: value for key, value in data.items() if key in _MEMBERSHIP_FIELDS}
        if values.get("schedule_mode") == "inherit":
            values["schedule_mode"] = None
            values["schedule_rule"] = None
        if values.get("schedule_mode") == "manual" or values.get("sync_enabled") is False:
            values["schedule_mode"] = "manual"
            values["sync_enabled"] = False
        elif "schedule_mode" in values:
            values["sync_enabled"] = True
            if values["schedule_mode"] != "calendar":
                values["schedule_rule"] = None
        elif values.get("sync_enabled") is True and member.schedule_mode == "manual":
            values["schedule_mode"] = None
        for key, value in values.items():
            setattr(member, key, value)
        await recompute_subscription_membership_cache(self.db, subscription_id)
        await self.db.flush()
        return await self.get(subscription_id)

    async def list_sources(self, subscription_id: UUID) -> list[SubscriptionSourceRead]:
        member = await self.require_membership(subscription_id)
        rows = (
            await self.db.execute(
                select(SubscriptionSource, UserSubscriptionSource)
                .join(
                    UserSubscriptionSource,
                    and_(
                        UserSubscriptionSource.subscription_source_id == SubscriptionSource.id,
                        UserSubscriptionSource.user_subscription_id == member.id,
                    ),
                )
                .where(SubscriptionSource.subscription_id == subscription_id)
                .order_by(SubscriptionSource.created_at, SubscriptionSource.id)
            )
        ).all()
        return [
            SubscriptionSourceRead.model_validate(
                {
                    "id": source.id,
                    "subscription_id": source.subscription_id,
                    "source": source.source,
                    "source_creator_id": source.source_creator_id,
                    "source_url": source.source_url,
                    "membership_source_id": binding.id,
                    "remote_account_id": binding.remote_account_id,
                    "is_enabled": binding.is_enabled,
                    "last_successful_auth": binding.last_successful_auth,
                    "auth_healthy": binding.auth_healthy,
                    "last_synced_at": binding.last_synced_at,
                    "last_attempted_at": binding.last_attempted_at,
                    "next_sync_at": binding.next_sync_at,
                    "auth_status": binding.auth_status,
                    "auth_error_reason": binding.auth_error_reason,
                    "last_auth_checked_at": binding.last_auth_checked_at,
                    "created_at": source.created_at,
                    "updated_at": binding.updated_at,
                }
            )
            for source, binding in rows
        ]

    async def update_source(self, subscription_id: UUID, source_id: UUID, data: dict[str, Any]) -> SubscriptionSourceRead:
        member = await self.require_membership(subscription_id)
        binding = (
            await self.db.execute(
                select(UserSubscriptionSource)
                .where(
                    UserSubscriptionSource.user_subscription_id == member.id,
                    UserSubscriptionSource.subscription_source_id == source_id,
                )
                .with_for_update(of=UserSubscriptionSource)
            )
        ).scalar_one_or_none()
        if binding is None:
            raise ValueError("Subscription source not found")
        if "is_enabled" in data and data["is_enabled"] is not None:
            binding.is_enabled = data["is_enabled"]
        if "remote_account_id" in data:
            account_id = data["remote_account_id"]
            if account_id is not None:
                account = (
                    await self.db.execute(
                        select(RemoteAccount).where(
                            RemoteAccount.id == account_id,
                            RemoteAccount.user_id == self.user_id,
                        )
                    )
                ).scalar_one_or_none()
                if account is None:
                    raise ValueError("Remote account not found")
                source = await self.db.get(SubscriptionSource, source_id)
                if source is None or account.source != source.source:
                    raise ValueError("Remote account source does not match subscription source")
            binding.remote_account_id = account_id
        await recompute_subscription_membership_cache(self.db, subscription_id)
        await self.db.flush()
        return next(item for item in await self.list_sources(subscription_id) if item.id == source_id)

    async def remove_source(self, subscription_id: UUID, source_id: UUID) -> None:
        member = await self.require_membership(subscription_id)
        deleted = await self.db.execute(
            delete(UserSubscriptionSource).where(
                UserSubscriptionSource.user_subscription_id == member.id,
                UserSubscriptionSource.subscription_source_id == source_id,
            )
        )
        if not deleted.rowcount:
            raise ValueError("Subscription source not found")
        await recompute_subscription_membership_cache(self.db, subscription_id)

    async def remove(self, subscription_id: UUID) -> None:
        member = await self.require_membership(subscription_id, lock=True)
        await self.db.execute(
            update(DiscoveryCandidate)
            .where(DiscoveryCandidate.user_subscription_id == member.id)
            .values(user_subscription_id=None)
        )
        await self.db.execute(
            delete(UserSubscriptionSource).where(UserSubscriptionSource.user_subscription_id == member.id)
        )
        await self.db.delete(member)
        await self.db.flush()
        await recompute_subscription_membership_cache(self.db, subscription_id)
