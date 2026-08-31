"""Persistent account-private discovery scans and idempotent imports."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from inspect import isawaitable
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, delete, false, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Creator,
    CreatorLink,
    DiscoveryCandidate,
    RemoteAccount,
    SourceCreator,
    Subscription,
    SubscriptionSource,
    TaskRun,
    UserSubscription,
)
from app.remote_discovery.classifier import IdentityMatchSignals, classify_identity
from app.remote_discovery.common import RemoteReauthenticationRequired
from app.remote_discovery.contract import RemoteCandidateIdentity
from app.remote_discovery.registry import DiscoveryAdapterRegistry, registry
from app.services.remote_accounts import (
    RemoteAccountService,
    RemoteCredentialGenerationChanged,
    configured_credential_vault,
)
from app.services.remote_credentials import CredentialVault
from app.services.remote_discovery_rollout import (
    RemoteDiscoveryUnavailable,
    auto_import_enabled,
    enabled_preview_sources,
    require_preview,
)
from app.services.settings import get_scheduler_config, get_subscription_defaults
from app.services.subscription_membership import (
    SubscriptionMembershipService,
    recompute_subscription_membership_cache,
)
from app.services.tasks import NONTERMINAL_STATUSES, TaskService


DISCOVERY_QUEUE = "discovery"
DISCOVERY_JOB_TIMEOUT = 3600


def discovery_rq_job_id(task_id: UUID, attempt: int) -> str:
    return f"discovery-{task_id}-attempt-{max(1, int(attempt))}"


async def prepare_discovery_scan_task(db: AsyncSession, task: TaskRun) -> TaskRun:
    task.attempts = max(0, int(task.attempts or 0)) + 1
    task.queue_name = DISCOVERY_QUEUE
    task.rq_job_id = discovery_rq_job_id(task.id, task.attempts)
    await db.flush()
    return task


def publish_discovery_scan(
    task_id: UUID,
    *,
    queue_name: str = DISCOVERY_QUEUE,
    attempt: int = 1,
):
    """Publish only opaque scan identity to the independent discovery queue."""

    from rq import Queue, Retry

    from app.jobs.remote_discovery import run_remote_discovery_scan
    from app.services.queue_admission import checked_enqueue
    from app.services.redis_client import get_redis

    queue = Queue(name=queue_name, connection=get_redis())
    return checked_enqueue(
        queue,
        run_remote_discovery_scan,
        str(task_id),
        job_id=discovery_rq_job_id(task_id, attempt),
        job_timeout=DISCOVERY_JOB_TIMEOUT,
        retry=Retry(max=3, interval=[60, 300, 900]),
    )


async def admit_due_remote_accounts(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 25,
    publisher=publish_discovery_scan,
) -> dict[str, Any]:
    """Claim due accounts, persist single-flight TaskRuns, then publish them."""

    now = now or _now()
    enabled_sources = enabled_preview_sources()
    if not enabled_sources:
        return {"created": 0, "published": 0, "task_ids": []}
    accounts = list(
        (
            await db.execute(
                select(RemoteAccount)
                .where(
                    RemoteAccount.is_enabled.is_(True),
                    RemoteAccount.auth_status == "healthy",
                    RemoteAccount.credential_ciphertext.is_not(None),
                    RemoteAccount.source.in_(enabled_sources),
                    or_(
                        RemoteAccount.next_scan_at.is_(None),
                        RemoteAccount.next_scan_at <= now,
                    ),
                )
                .order_by(RemoteAccount.next_scan_at.asc().nullsfirst(), RemoteAccount.id)
                .limit(max(1, min(int(limit), 200)))
                .with_for_update(of=RemoteAccount, skip_locked=True)
            )
        ).scalars()
    )
    tasks: list[TaskRun] = []
    service = RemoteDiscoveryService(db)
    for account in accounts:
        try:
            task = await service.create_scan(account.user_id, account.id)
        except DiscoveryScanInProgress:
            continue
        await prepare_discovery_scan_task(db, task)
        tasks.append(task)
    if tasks:
        await db.commit()

    published = 0
    for task in tasks:
        try:
            result = publisher(task.id, queue_name=DISCOVERY_QUEUE)
            if isawaitable(result):
                await result
            published += 1
        except Exception as exc:
            current = await db.get(TaskRun, task.id)
            if current is not None:
                await TaskService(db).update_task(
                    current,
                    status="failed",
                    error=f"Discovery enqueue failed ({type(exc).__name__})",
                    reason_code="discovery_enqueue_failed",
                )
                account = await db.get(RemoteAccount, current.triggering_remote_account_id)
                if account is not None:
                    account.next_scan_at = now + timedelta(minutes=5)
                await db.commit()
    return {
        "created": len(tasks),
        "published": published,
        "task_ids": [str(task.id) for task in tasks],
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(child) for child in value]
    return value


def _advisory_lock_key(namespace: str, *parts: object) -> int:
    """Return a stable, namespaced signed bigint transaction-lock key."""

    encoded = "\0".join([namespace, *(str(part) for part in parts)])
    digest = hashlib.sha256(encoded.encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _shared_identity_lock_key(source: str, source_creator_id: str) -> int:
    return _advisory_lock_key("remote-identity", source, source_creator_id)


def _creator_lock_key(creator_id: UUID) -> int:
    return _advisory_lock_key("creator", creator_id)


class DiscoveryScanInProgress(ValueError):
    def __init__(self, task_id: UUID):
        self.task_id = task_id
        super().__init__(f"Discovery scan already running: {task_id}")


class RemoteDiscoveryService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        vault: CredentialVault | None = None,
        adapters: DiscoveryAdapterRegistry | None = None,
    ):
        self.db = db
        # Account admission and list operations never decrypt credentials.
        # Resolve the separately managed key only at the execution boundary.
        self.vault = vault
        self.adapters = adapters or registry

    async def _lock_remote_identity(self, source: str, source_creator_id: str) -> None:
        await self.db.execute(
            select(
                func.pg_advisory_xact_lock(
                    _shared_identity_lock_key(source, source_creator_id)
                )
            )
        )

    async def _lock_creator(self, creator_id: UUID) -> None:
        await self.db.execute(
            select(func.pg_advisory_xact_lock(_creator_lock_key(creator_id)))
        )

    async def _owned_account(self, user_id: int, account_id: UUID, *, lock: bool = False) -> RemoteAccount:
        stmt = select(RemoteAccount).where(
            RemoteAccount.id == account_id,
            RemoteAccount.user_id == user_id,
        )
        if lock:
            stmt = stmt.with_for_update(of=RemoteAccount)
        account = (await self.db.execute(stmt)).scalar_one_or_none()
        if account is None:
            raise ValueError("Remote account not found")
        return account

    @staticmethod
    def _remote_identity(account: RemoteAccount) -> dict[str, Any]:
        return {
            "account_id": str(account.id),
            "auth_method": account.auth_method,
            "remote_user_id": account.remote_user_id,
            "source": account.source,
        }

    async def _locked_pinned_account(
        self,
        account_id: UUID,
        *,
        generation: int,
        remote_identity: dict[str, Any],
    ) -> RemoteAccount:
        account = (
            await self.db.execute(
                select(RemoteAccount)
                .where(RemoteAccount.id == account_id)
                .with_for_update(of=RemoteAccount)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if account is None:
            raise RemoteCredentialGenerationChanged(
                "remote account was removed during discovery"
            )
        if (
            int(account.credential_generation or 0) != generation
            or self._remote_identity(account) != remote_identity
        ):
            raise RemoteCredentialGenerationChanged(
                "remote account credentials changed during discovery"
            )
        return account

    async def create_scan(self, user_id: int, account_id: UUID) -> TaskRun:
        account = await self._owned_account(user_id, account_id, lock=True)
        require_preview(account.source)
        active = (
            await self.db.execute(
                select(TaskRun)
                .where(
                    TaskRun.triggering_remote_account_id == account.id,
                    TaskRun.operation_type == "remote-discovery-scan",
                    TaskRun.status.in_(NONTERMINAL_STATUSES),
                )
                .with_for_update(of=TaskRun)
                .limit(1)
            )
        ).scalar_one_or_none()
        if active is not None:
            raise DiscoveryScanInProgress(active.id)

        selectors = account.collection_selectors or [{}]
        resume = account.scan_cursor if isinstance(account.scan_cursor, dict) else None
        remote_identity = self._remote_identity(account)
        credential_generation = int(account.credential_generation or 0)
        if resume and (
            resume.get("credential_generation") != credential_generation
            or resume.get("remote_identity") != remote_identity
        ):
            resume = None
            account.scan_cursor = None
        started_at = (
            datetime.fromisoformat(str(resume["started_at"]))
            if resume and resume.get("started_at")
            else _now()
        )
        selector_index = int((resume or {}).get("selector_index") or 0)
        cursor = (resume or {}).get("cursor")
        progress = {
            "phase": "queued",
            "selector_index": selector_index,
            "selector_count": len(selectors),
            "cursor": _json_value(cursor) if cursor else None,
            "pages_completed": int((resume or {}).get("pages_completed") or 0),
            "candidates_seen": int((resume or {}).get("candidates_seen") or 0),
            "scan_started_at": _utc(started_at).isoformat(),
            "credential_generation": credential_generation,
            "remote_identity": remote_identity,
        }
        task = await TaskService(self.db).create_task(
            kind="discovery",
            operation_type="remote-discovery-scan",
            title=f"Discover {account.source} followings",
            status="enqueued",
            queue_name="discovery",
            source=account.source,
            progress=progress,
            meta={"account_id": str(account.id), "scope": "remote_account"},
            triggering_remote_account_id=account.id,
            owner_user_id=account.user_id,
        )
        account.last_scan_started_at = _utc(started_at)
        return task

    async def list_scans(
        self,
        user_id: int,
        *,
        account_id: UUID | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[int, list[TaskRun]]:
        owner_filter = or_(
            TaskRun.owner_user_id == user_id,
            and_(
                TaskRun.owner_user_id.is_(None),
                RemoteAccount.user_id == user_id,
            ),
        )
        stmt = (
            select(TaskRun)
            .outerjoin(
                RemoteAccount,
                RemoteAccount.id == TaskRun.triggering_remote_account_id,
            )
            .where(
                owner_filter,
                TaskRun.operation_type == "remote-discovery-scan",
            )
        )
        count_stmt = (
            select(func.count(TaskRun.id))
            .outerjoin(
                RemoteAccount,
                RemoteAccount.id == TaskRun.triggering_remote_account_id,
            )
            .where(
                owner_filter,
                TaskRun.operation_type == "remote-discovery-scan",
            )
        )
        if account_id is not None:
            stmt = stmt.where(TaskRun.triggering_remote_account_id == account_id)
            count_stmt = count_stmt.where(TaskRun.triggering_remote_account_id == account_id)
        total = (await self.db.execute(count_stmt)).scalar_one()
        tasks = (
            await self.db.execute(
                stmt.order_by(TaskRun.created_at.desc(), TaskRun.id)
                .offset(max(0, offset))
                .limit(max(1, min(limit, 200)))
            )
        ).scalars().all()
        return int(total), list(tasks)

    async def _local_creator_matches(
        self,
        account: RemoteAccount,
        identity: RemoteCandidateIdentity,
        *,
        additional_creator_ids: set[UUID] | None = None,
    ) -> tuple[set[UUID], bool, bool]:
        creator_ids = set(additional_creator_ids or ())
        exact = (
            await self.db.execute(
                select(SourceCreator.creator_id).where(
                    SourceCreator.source == account.source,
                    SourceCreator.source_creator_id == identity.source_creator_id,
                    SourceCreator.creator_id.is_not(None),
                )
            )
        ).scalars().all()
        creator_ids.update(creator_id for creator_id in exact if creator_id is not None)

        metadata = _json_value(identity.metadata)
        links = [identity.profile_url] if identity.profile_url else []
        for key in ("supported_links", "links", "profile_links"):
            value = metadata.get(key)
            if isinstance(value, list):
                links.extend(str(item) for item in value if item)
        verified_cross_site = False
        if links:
            verified = (
                await self.db.execute(
                    select(CreatorLink.creator_id).where(
                        CreatorLink.is_verified.is_(True),
                        CreatorLink.url.in_(links),
                    )
                )
            ).scalars().all()
            verified_cross_site = bool(verified)
            creator_ids.update(verified)

        danbooru_match = False
        danbooru_artist_id = metadata.get("danbooru_artist_id")
        if danbooru_artist_id is not None:
            try:
                danbooru_artist_id = int(danbooru_artist_id)
            except (TypeError, ValueError):
                danbooru_artist_id = None
            if danbooru_artist_id is not None:
                danbooru_ids = (
                    await self.db.execute(
                        select(Creator.id).where(Creator.danbooru_artist_id == danbooru_artist_id)
                    )
                ).scalars().all()
                danbooru_match = bool(danbooru_ids)
                creator_ids.update(danbooru_ids)
        return creator_ids, verified_cross_site, danbooru_match

    async def upsert_candidate(
        self,
        account: RemoteAccount,
        identity: RemoteCandidateIdentity,
        *,
        seen_at: datetime,
        additional_creator_ids: set[UUID] | None = None,
    ) -> DiscoveryCandidate:
        if identity.source != account.source:
            raise ValueError("Discovery candidate source does not match remote account")
        creator_ids, verified_link, danbooru_match = await self._local_creator_matches(
            account,
            identity,
            additional_creator_ids=additional_creator_ids,
        )
        metadata = _json_value(identity.metadata)
        signals = IdentityMatchSignals(
            source=account.source,
            local_identity_match_count=len(creator_ids),
            danbooru_verified_link=danbooru_match,
            verified_cross_site_link=verified_link,
            pixiv_illustration_preview=bool(
                metadata.get("has_illustration_preview")
                or metadata.get("illustration_preview")
                or metadata.get("illusts")
            ),
            art_focused_bio=bool(metadata.get("art_focused_bio")),
            recent_visual_post=bool(
                metadata.get("recent_visual_post")
                or metadata.get("has_recent_visual_content")
            ),
            supported_site_link=bool(
                metadata.get("supported_site_link")
                or metadata.get("supported_links")
            ),
        )
        classification = classify_identity(signals)
        candidate = (
            await self.db.execute(
                select(DiscoveryCandidate)
                .where(
                    DiscoveryCandidate.remote_account_id == account.id,
                    DiscoveryCandidate.source_creator_id == identity.source_creator_id,
                )
                .with_for_update(of=DiscoveryCandidate)
            )
        ).scalar_one_or_none()
        state = "conflict" if classification.identity_conflict else "pending"
        snapshot = {
            **metadata,
            "username": identity.username,
            "local_creator_ids": sorted(str(item) for item in creator_ids),
            "identity_conflict": classification.identity_conflict,
        }
        if candidate is None:
            candidate = DiscoveryCandidate(
                remote_account_id=account.id,
                user_id=account.user_id,
                source_creator_id=identity.source_creator_id,
                remote_url=identity.profile_url,
                display_name=identity.display_name,
                candidate_metadata=snapshot,
                confidence=classification.confidence,
                confidence_reasons=list(classification.reasons),
                state=state,
                last_seen_at=seen_at,
                is_following=True,
            )
            self.db.add(candidate)
        else:
            candidate.remote_url = identity.profile_url
            candidate.display_name = identity.display_name
            candidate.candidate_metadata = snapshot
            candidate.confidence = classification.confidence
            candidate.confidence_reasons = list(classification.reasons)
            candidate.last_seen_at = seen_at
            candidate.is_following = True
            if candidate.state not in {"dismissed", "imported"}:
                candidate.state = state
        await self.db.flush()
        return candidate

    async def run_scan(self, task_id: UUID) -> TaskRun:
        queued_source = (
            await self.db.execute(
                select(RemoteAccount.source)
                .join(TaskRun, TaskRun.triggering_remote_account_id == RemoteAccount.id)
                .where(
                    TaskRun.id == task_id,
                    TaskRun.operation_type == "remote-discovery-scan",
                )
            )
        ).scalar_one_or_none()
        if queued_source is not None:
            try:
                require_preview(queued_source)
            except RemoteDiscoveryUnavailable as exc:
                task = await self.db.get(TaskRun, task_id)
                if task is None:
                    raise ValueError("Discovery scan task not found") from exc
                if task.status in NONTERMINAL_STATUSES:
                    await TaskService(self.db).update_task(
                        task,
                        status="failed",
                        error="Discovery scan disabled by rollout",
                        reason_code=exc.code,
                    )
                    await self.db.commit()
                return task
        claimed_at = _now()
        claimed = (
            await self.db.execute(
                update(TaskRun)
                .where(
                    TaskRun.id == task_id,
                    TaskRun.operation_type == "remote-discovery-scan",
                    TaskRun.status.in_({"enqueued", "recovering"}),
                )
                .values(
                    status="running",
                    resource_state="running",
                    started_at=func.coalesce(TaskRun.started_at, claimed_at),
                    finished_at=None,
                    last_heartbeat_at=claimed_at,
                    updated_at=claimed_at,
                )
                .returning(TaskRun.id, TaskRun.triggering_remote_account_id)
            )
        ).first()
        if claimed is None:
            task = await self.db.get(TaskRun, task_id)
            if task is None or task.operation_type != "remote-discovery-scan":
                raise ValueError("Discovery scan task not found")
            if task.status == "complete":
                return task
            if task.status == "running":
                raise DiscoveryScanInProgress(task.id)
            # Stale/failed scans are deliberately not self-claimed: normal task
            # recovery must transition them back to enqueued first, preserving
            # retry accounting and preventing an abandoned worker from reviving
            # itself behind the control plane.
            raise ValueError("Discovery scan must be re-enqueued before execution")

        task_service = TaskService(self.db)

        async def fail_before_provider(exc: Exception) -> None:
            """Durably terminalize a claimed task when setup cannot begin."""

            await self.db.rollback()
            failed_task = await self.db.get(TaskRun, task_id)
            if failed_task is not None and failed_task.status in NONTERMINAL_STATUSES:
                await task_service.update_task(
                    failed_task,
                    status="failed",
                    progress=dict(failed_task.progress_data or {}),
                    error=f"Discovery scan failed ({type(exc).__name__})",
                    reason_code=(
                        "remote_credential_changed"
                        if isinstance(exc, RemoteCredentialGenerationChanged)
                        else "remote_discovery_failed"
                    ),
                )
                await self.db.commit()
            raise exc

        account_id = claimed.triggering_remote_account_id
        if account_id is None:
            await fail_before_provider(ValueError("Discovery scan task has no account"))
        account = (
            await self.db.execute(
                select(RemoteAccount)
                .where(RemoteAccount.id == account_id)
                .with_for_update(of=RemoteAccount)
            )
        ).scalar_one_or_none()
        if account is None:
            await fail_before_provider(ValueError("Remote account not found"))
        task = await self.db.get(TaskRun, task_id, populate_existing=True)
        progress = dict(task.progress_data or {})
        pinned_generation = progress.get("credential_generation")
        pinned_identity = progress.get("remote_identity")
        current_identity = self._remote_identity(account)
        current_generation = int(account.credential_generation or 0)
        if (
            not isinstance(pinned_generation, int)
            or not isinstance(pinned_identity, dict)
        ):
            pinned_generation = current_generation
            pinned_identity = current_identity
            progress.update(
                {
                    "credential_generation": pinned_generation,
                    "remote_identity": pinned_identity,
                }
            )
        elif (
            pinned_generation != current_generation
            or pinned_identity != current_identity
        ):
            if progress.get("phase") == "queued":
                await fail_before_provider(
                    RemoteCredentialGenerationChanged(
                        "remote account credentials changed before discovery started"
                    )
                )
            # An explicitly recovered task restarts a full scan at the current
            # generation; carrying a prior-generation cursor would mix pages.
            pinned_generation = current_generation
            pinned_identity = current_identity
            progress = {
                **progress,
                "phase": "queued",
                "selector_index": 0,
                "cursor": None,
                "pages_completed": 0,
                "candidates_seen": 0,
                "scan_started_at": _utc(_now()).isoformat(),
                "credential_generation": pinned_generation,
                "remote_identity": pinned_identity,
            }
            account.scan_cursor = None
        selectors = account.collection_selectors or [{}]
        selector_index = int(progress.get("selector_index") or 0)
        cursor = progress.get("cursor")
        started_at = datetime.fromisoformat(str(progress["scan_started_at"]))
        pages_completed = int(progress.get("pages_completed") or 0)
        seen_count = int(progress.get("candidates_seen") or 0)
        await task_service.add_event(
            task,
            "status_changed",
            from_status="enqueued",
            to_status="running",
            message="Discovery scan worker claimed task",
        )
        await task_service.update_task(
            task,
            progress={**progress, "phase": "fetching"},
            resource_state="running",
        )
        await self.db.commit()

        try:
            # Credential-key resolution is part of execution, not admission.
            # Keep it inside the failure-finalization boundary so a missing or
            # wrong deployment key cannot strand the already claimed task.
            account_service = RemoteAccountService(
                self.db,
                account.user_id,
                vault=self.vault or configured_credential_vault(),
                adapters=self.adapters,
            )
            while selector_index < len(selectors):
                account = await self._locked_pinned_account(
                    account.id,
                    generation=pinned_generation,
                    remote_identity=pinned_identity,
                )

                async def advance_generation(new_generation: int) -> None:
                    nonlocal pinned_generation, progress

                    if self._remote_identity(account) != pinned_identity:
                        raise RemoteCredentialGenerationChanged(
                            "remote account identity changed during OAuth refresh"
                        )
                    pinned_generation = new_generation
                    progress = {
                        **progress,
                        "credential_generation": new_generation,
                        "remote_identity": pinned_identity,
                    }
                    task_for_pin = await self.db.get(TaskRun, task_id)
                    task_for_pin.progress_data = progress
                    if isinstance(account.scan_cursor, dict):
                        account.scan_cursor = {
                            **account.scan_cursor,
                            "credential_generation": new_generation,
                            "remote_identity": pinned_identity,
                        }

                credentials = account_service.credentials_for_adapter(
                    account,
                    expected_generation=pinned_generation,
                    on_generation_advanced=advance_generation,
                )
                # Do not hold a database row lock across ordinary provider I/O.
                # A replacement during fetch is detected before any page write.
                await self.db.commit()
                page = await self.adapters.get(account.source).fetch_page(
                    credentials,
                    selector=selectors[selector_index],
                    cursor=cursor,
                    page_size=100,
                )
                account = await self._locked_pinned_account(
                    account.id,
                    generation=pinned_generation,
                    remote_identity=pinned_identity,
                )
                seen_at = _now()
                for identity in page.items:
                    await self.upsert_candidate(account, identity, seen_at=seen_at)
                seen_count += len(page.items)
                pages_completed += 1
                if page.done:
                    selector_index += 1
                    cursor = None
                else:
                    cursor = _json_value(page.next_cursor)
                checkpoint = {
                    "selector_index": selector_index,
                    "cursor": cursor,
                    "pages_completed": pages_completed,
                    "candidates_seen": seen_count,
                    "started_at": _utc(started_at).isoformat(),
                    "credential_generation": pinned_generation,
                    "remote_identity": pinned_identity,
                }
                progress = {
                    "phase": "fetching" if selector_index < len(selectors) else "finalizing",
                    "selector_index": selector_index,
                    "selector_count": len(selectors),
                    "cursor": cursor,
                    "pages_completed": pages_completed,
                    "candidates_seen": seen_count,
                    "scan_started_at": _utc(started_at).isoformat(),
                    "credential_generation": pinned_generation,
                    "remote_identity": pinned_identity,
                }
                account.scan_cursor = checkpoint if selector_index < len(selectors) else None
                task = await self.db.get(TaskRun, task_id)
                task.last_heartbeat_at = seen_at
                await task_service.update_task(task, progress=progress)
                # Candidate snapshots and their cursor are one page transaction.
                await self.db.commit()

            account = await self._locked_pinned_account(
                account.id,
                generation=pinned_generation,
                remote_identity=pinned_identity,
            )
            await self.db.execute(
                update(DiscoveryCandidate)
                .where(
                    DiscoveryCandidate.remote_account_id == account.id,
                    or_(
                        DiscoveryCandidate.last_seen_at.is_(None),
                        DiscoveryCandidate.last_seen_at < _utc(started_at),
                    ),
                )
                .values(is_following=False)
            )
            account.scan_cursor = None
            account.last_scan_completed_at = _now()
            account.next_scan_at = account.last_scan_completed_at + timedelta(
                hours=account.scan_interval_hours
            )
            auto_imported = []
            if account.auto_import_enabled:
                auto_imported = await self.auto_import(account)
            task = await self.db.get(TaskRun, task_id)
            await task_service.update_task(
                task,
                status="complete",
                progress={**progress, "phase": "complete", "cursor": None},
                result={
                    "candidates_seen": seen_count,
                    "pages_completed": pages_completed,
                    "auto_imported_count": len(auto_imported),
                },
            )
            await self.db.commit()
            return task
        except Exception as exc:
            await self.db.rollback()
            task = await self.db.get(TaskRun, task_id)
            if isinstance(exc, RemoteReauthenticationRequired):
                try:
                    account = await self._locked_pinned_account(
                        task.triggering_remote_account_id,
                        generation=pinned_generation,
                        remote_identity=pinned_identity,
                    )
                except RemoteCredentialGenerationChanged:
                    account = None
                if account is not None:
                    account.auth_status = "unhealthy"
                    account.auth_error_reason = "reauthentication_required"
                    await account_service._set_binding_health(
                        account,
                        healthy=False,
                        failure_reason="reauthentication_required",
                    )
            await TaskService(self.db).update_task(
                task,
                status="failed",
                progress=dict(task.progress_data or progress),
                error=f"Discovery scan failed ({type(exc).__name__})",
                reason_code=(
                    "remote_credential_changed"
                    if isinstance(exc, RemoteCredentialGenerationChanged)
                    else "remote_discovery_failed"
                ),
            )
            await self.db.commit()
            raise

    async def list_candidates(
        self,
        user_id: int,
        *,
        account_id: UUID | None = None,
        state: str | None = None,
        confidence: str | None = None,
        is_following: bool | None = None,
        local_match: bool | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[int, list[DiscoveryCandidate]]:
        filters = [DiscoveryCandidate.user_id == user_id]
        if account_id is not None:
            filters.append(DiscoveryCandidate.remote_account_id == account_id)
        if state:
            filters.append(DiscoveryCandidate.state == state)
        if confidence:
            filters.append(DiscoveryCandidate.confidence == confidence)
        if is_following is not None:
            filters.append(DiscoveryCandidate.is_following.is_(is_following))
        if local_match is not None:
            local_creator_ids = DiscoveryCandidate.candidate_metadata["local_creator_ids"]
            has_local_creator_ids = case(
                (
                    func.jsonb_typeof(local_creator_ids) == "array",
                    func.jsonb_array_length(local_creator_ids) > 0,
                ),
                else_=false(),
            )
            matched = or_(
                DiscoveryCandidate.subscription_id.is_not(None),
                has_local_creator_ids,
            )
            filters.append(matched if local_match else ~matched)
        total = (
            await self.db.execute(select(func.count(DiscoveryCandidate.id)).where(*filters))
        ).scalar_one()
        candidates = (
            await self.db.execute(
                select(DiscoveryCandidate)
                .where(*filters)
                .order_by(DiscoveryCandidate.updated_at.desc(), DiscoveryCandidate.id)
                .offset(max(0, offset))
                .limit(max(1, min(limit, 200)))
            )
        ).scalars().all()
        return int(total), list(candidates)

    async def _candidate(self, user_id: int, candidate_id: UUID, *, lock: bool = False) -> DiscoveryCandidate:
        stmt = select(DiscoveryCandidate).where(
            DiscoveryCandidate.id == candidate_id,
            DiscoveryCandidate.user_id == user_id,
        )
        if lock:
            stmt = stmt.with_for_update(of=DiscoveryCandidate).execution_options(
                populate_existing=True
            )
        candidate = (await self.db.execute(stmt)).scalar_one_or_none()
        if candidate is None:
            raise ValueError("Discovery candidate not found")
        return candidate

    async def batch_action(
        self,
        user_id: int,
        candidate_ids: list[UUID],
        *,
        action: str,
    ) -> list[DiscoveryCandidate]:
        if not candidate_ids or len(candidate_ids) > 200:
            raise ValueError("Candidate batch must contain between 1 and 200 IDs")
        results = []
        for candidate_id in dict.fromkeys(candidate_ids):
            if action == "import":
                # Import has its own RemoteAccount -> Candidate lock prefix.
                # Taking Candidate here would invert account deletion's order.
                results.append(await self.import_candidate(user_id, candidate_id))
                continue
            candidate = await self._candidate(user_id, candidate_id, lock=True)
            if action == "dismiss":
                if candidate.state != "imported":
                    candidate.state = "dismissed"
                    candidate.dismissed_at = _now()
            elif action == "restore":
                if candidate.state == "dismissed":
                    metadata = candidate.candidate_metadata or {}
                    candidate.state = "conflict" if metadata.get("identity_conflict") else "pending"
                    candidate.dismissed_at = None
            else:
                raise ValueError("Unknown candidate batch action")
            results.append(candidate)
        await self.db.flush()
        return results

    async def _resolved_creator(self, candidate: DiscoveryCandidate) -> Creator:
        metadata = candidate.candidate_metadata or {}
        local_ids = []
        for value in metadata.get("local_creator_ids") or []:
            try:
                local_ids.append(UUID(str(value)))
            except ValueError:
                continue
        resolved = metadata.get("resolved_creator_id")
        if resolved:
            local_ids = [UUID(str(resolved))]
        source_creator = (
            await self.db.execute(
                select(SourceCreator).where(
                    SourceCreator.source == (
                        await self.db.get(RemoteAccount, candidate.remote_account_id)
                    ).source,
                    SourceCreator.source_creator_id == candidate.source_creator_id,
                )
            )
        ).scalar_one_or_none()
        if source_creator and source_creator.creator_id:
            local_ids = [source_creator.creator_id]
        local_ids = list(dict.fromkeys(local_ids))
        if len(local_ids) > 1:
            raise ValueError("Discovery candidate has an unresolved identity conflict")
        if local_ids:
            creator = await self.db.get(Creator, local_ids[0])
            if creator is not None:
                return creator
        creator = Creator(name=candidate.display_name or candidate.source_creator_id)
        self.db.add(creator)
        await self.db.flush()
        return creator

    async def import_candidate(self, user_id: int, candidate_id: UUID) -> DiscoveryCandidate:
        # Snapshot only immutable identifiers before locking. Account lifecycle
        # paths serialize on RemoteAccount first, so import must wait there
        # before taking Candidate or member-source locks.
        candidate_snapshot = await self._candidate(user_id, candidate_id)
        snapshot_account_id = candidate_snapshot.remote_account_id
        snapshot_source_creator_id = candidate_snapshot.source_creator_id
        snapshot_state = candidate_snapshot.state
        account_snapshot = await self._owned_account(user_id, snapshot_account_id)
        snapshot_source = account_snapshot.source
        require_preview(snapshot_source)
        snapshot_generation = account_snapshot.credential_generation

        membership_service = SubscriptionMembershipService(self.db, user_id)
        account = await membership_service._lock_remote_account_for_source(
            snapshot_account_id,
            snapshot_source,
            expected_generation=snapshot_generation,
        )
        candidate = await self._candidate(user_id, candidate_id, lock=True)
        if (
            candidate.remote_account_id != snapshot_account_id
            or candidate.source_creator_id != snapshot_source_creator_id
            or candidate.state != snapshot_state
        ):
            raise ValueError("Discovery candidate changed during import")
        if candidate.state == "dismissed":
            raise ValueError("Dismissed candidate must be restored before import")
        if candidate.state == "conflict" or (candidate.candidate_metadata or {}).get("identity_conflict"):
            raise ValueError("Discovery candidate has an unresolved conflict")
        # All shared rows derived from one remote identity must converge even
        # when two users import it in separate transactions at the same time.
        # The lock is released automatically at transaction end.
        # Lock order is always remote identity, then Creator.  No path in this
        # service takes these locks in the reverse order.
        await self._lock_remote_identity(account.source, candidate.source_creator_id)
        creator = await self._resolved_creator(candidate)
        await self._lock_creator(creator.id)
        source_creator = (
            await self.db.execute(
                select(SourceCreator).where(
                    SourceCreator.source == account.source,
                    SourceCreator.source_creator_id == candidate.source_creator_id,
                )
            )
        ).scalar_one_or_none()
        if source_creator is None:
            source_creator = SourceCreator(
                creator_id=creator.id,
                source=account.source,
                source_creator_id=candidate.source_creator_id,
                source_url=candidate.remote_url,
                display_name=candidate.display_name,
                raw_metadata=candidate.candidate_metadata,
            )
            self.db.add(source_creator)
        elif source_creator.creator_id is None:
            source_creator.creator_id = creator.id
        await self.db.flush()
        subscription = (
            await self.db.execute(
                select(Subscription).where(Subscription.creator_id == creator.id).limit(1)
            )
        ).scalar_one_or_none()
        if subscription is None:
            subscription = Subscription(
                creator_id=creator.id,
                name=creator.display_name or creator.name,
                is_active=True,
                sync_enabled=True,
                sync_interval_hours=6,
            )
            self.db.add(subscription)
            await self.db.flush()
        source_identity = [
            SubscriptionSource.source_creator_id == candidate.source_creator_id
        ]
        if candidate.remote_url:
            source_identity.append(SubscriptionSource.source_url == candidate.remote_url)
        canonical_source = (
            await self.db.execute(
                select(SubscriptionSource).where(
                    SubscriptionSource.subscription_id == subscription.id,
                    SubscriptionSource.source == account.source,
                    or_(*source_identity),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if canonical_source is None:
            canonical_source = SubscriptionSource(
                subscription_id=subscription.id,
                source=account.source,
                source_creator_id=candidate.source_creator_id,
                source_url=candidate.remote_url,
                is_enabled=True,
            )
            self.db.add(canonical_source)
            await self.db.flush()
        member = (
            await self.db.execute(
                select(UserSubscription)
                .where(
                    UserSubscription.user_id == user_id,
                    UserSubscription.subscription_id == subscription.id,
                )
                .with_for_update(of=UserSubscription)
            )
        ).scalar_one_or_none()
        if member is None:
            defaults = await get_subscription_defaults(self.db)
            member = await membership_service.ensure_membership(
                subscription,
                name=creator.display_name or creator.name,
                **defaults,
            )
        binding, _created = await membership_service._ensure_source_binding(
            member,
            canonical_source,
            remote_account_id=account.id,
            remote_account_generation=account.credential_generation,
            locked_remote_account=account,
            is_enabled=None,
        )
        candidate.state = "imported"
        candidate.subscription_id = subscription.id
        candidate.user_subscription_id = member.id
        candidate.imported_at = candidate.imported_at or _now()
        candidate.dismissed_at = None
        await recompute_subscription_membership_cache(self.db, subscription.id)
        await self.db.flush()
        return candidate

    async def resolve_candidate(
        self,
        user_id: int,
        candidate_id: UUID,
        *,
        creator_id: UUID | None = None,
        creator_name: str | None = None,
    ) -> DiscoveryCandidate:
        candidate = await self._candidate(user_id, candidate_id, lock=True)
        if candidate.state != "conflict":
            raise ValueError("Discovery candidate is not in conflict")
        account = await self._owned_account(user_id, candidate.remote_account_id)
        require_preview(account.source)
        # Identity is always acquired before Creator, matching import_candidate
        # and preventing two resolutions from installing incompatible mappings.
        await self._lock_remote_identity(account.source, candidate.source_creator_id)
        source_creator = (
            await self.db.execute(
                select(SourceCreator).where(
                    SourceCreator.source == account.source,
                    SourceCreator.source_creator_id == candidate.source_creator_id,
                )
            )
        ).scalar_one_or_none()
        if creator_id is None:
            if source_creator is not None and source_creator.creator_id is not None:
                raise ValueError("Remote identity was already resolved to a creator")
            creator = Creator(
                name=creator_name or candidate.display_name or candidate.source_creator_id
            )
            self.db.add(creator)
            await self.db.flush()
            creator_id = creator.id
        elif await self.db.get(Creator, creator_id) is None:
            raise ValueError("Creator not found")
        await self._lock_creator(creator_id)
        if (
            source_creator is not None
            and source_creator.creator_id is not None
            and source_creator.creator_id != creator_id
        ):
            raise ValueError("Remote identity was already resolved to a different creator")
        metadata = dict(candidate.candidate_metadata or {})
        metadata["resolved_creator_id"] = str(creator_id)
        metadata["local_creator_ids"] = [str(creator_id)]
        metadata["identity_conflict"] = False
        if source_creator is None:
            self.db.add(
                SourceCreator(
                    creator_id=creator_id,
                    source=account.source,
                    source_creator_id=candidate.source_creator_id,
                    source_url=candidate.remote_url,
                    display_name=candidate.display_name,
                    raw_metadata=metadata,
                )
            )
        else:
            source_creator.creator_id = creator_id
        candidate.candidate_metadata = metadata
        candidate.state = "pending"
        candidate.confidence = "high"
        candidate.confidence_reasons = ["manually_resolved_identity"]
        await self.db.flush()
        return candidate

    async def auto_import(self, account: RemoteAccount) -> list[DiscoveryCandidate]:
        if not auto_import_enabled(account.source):
            return []
        allowed = {
            "high": {"high"},
            "medium": {"high", "medium"},
            "low": {"high", "medium", "low"},
        }[account.auto_import_min_confidence]
        candidates = (
            await self.db.execute(
                select(DiscoveryCandidate)
                .where(
                    DiscoveryCandidate.remote_account_id == account.id,
                    DiscoveryCandidate.state == "pending",
                    DiscoveryCandidate.is_following.is_(True),
                    DiscoveryCandidate.confidence.in_(allowed),
                )
                .order_by(DiscoveryCandidate.created_at, DiscoveryCandidate.id)
                .limit(account.auto_import_limit)
            )
        ).scalars().all()
        imported = []
        for candidate in candidates:
            imported.append(await self.import_candidate(account.user_id, candidate.id))
        return imported
