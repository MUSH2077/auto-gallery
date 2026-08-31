"""User-bound remote creator detail browsing and ticket issuance."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DiscoveryCandidate, DownloadJob, RemoteAccount, WorkSource
from app.remote_discovery.common import RemoteReauthenticationRequired
from app.remote_discovery.contract import (
    RemoteCreatorDetail,
    RemoteCreatorProfile,
    RemoteWorkPage,
    RemoteWorkPreview,
)
from app.remote_discovery.registry import DiscoveryAdapterRegistry, registry
from app.schemas.remote_discovery import (
    DiscoveryCandidateRead,
    DiscoveryRecentWorkRead,
    RemoteCreatorDetailRead,
    RemoteCreatorProfileRead,
    RemoteWorkPageRead,
    RemoteWorkPreviewRead,
)
from app.services.remote_access_tokens import RemoteAccessTokenError, RemoteAccessTokenService
from app.services.remote_accounts import (
    RemoteAccountService,
    RemoteCredentialGenerationChanged,
    RemoteWorkStateAccountRequired,
    RemoteWorkStateAccountUnhealthy,
    configured_credential_vault,
)
from app.services.remote_credentials import CredentialVault
from app.services.remote_discovery_rollout import require_preview


ACTIVE_DOWNLOAD_STATUSES = {"pending", "enqueued", "downloading", "downloaded", "importing"}


class RemoteCreatorAccessService:
    """Read volatile provider data without persisting it as identity mapping."""

    def __init__(
        self,
        db: AsyncSession,
        user_id: int,
        *,
        vault: CredentialVault | None = None,
        adapters: DiscoveryAdapterRegistry | None = None,
        tokens: RemoteAccessTokenService | None = None,
    ) -> None:
        self.db = db
        self.user_id = user_id
        # Scan snapshots only need ticket signing; defer vault validation until
        # a live provider call actually needs credential decryption.
        self.vault = vault
        self.adapters = adapters or registry
        self.tokens = tokens or RemoteAccessTokenService()

    async def _context(self, candidate_id: UUID) -> tuple[DiscoveryCandidate, RemoteAccount]:
        row = (
            await self.db.execute(
                select(DiscoveryCandidate, RemoteAccount)
                .join(RemoteAccount, RemoteAccount.id == DiscoveryCandidate.remote_account_id)
                .where(
                    DiscoveryCandidate.id == candidate_id,
                    DiscoveryCandidate.user_id == self.user_id,
                    RemoteAccount.user_id == self.user_id,
                )
            )
        ).first()
        if row is None:
            raise ValueError("Discovery candidate not found")
        candidate, account = row
        require_preview(account.source)
        if account.source != "pixiv":
            raise NotImplementedError(f"{account.source} does not support remote creator details")
        if not account.is_enabled or not account.credential_ciphertext:
            raise RemoteWorkStateAccountRequired
        if account.auth_status != "healthy":
            raise RemoteWorkStateAccountUnhealthy
        return candidate, account

    async def present_candidates(
        self, candidates: list[DiscoveryCandidate]
    ) -> list[DiscoveryCandidateRead]:
        """Expose only signed media references from scan-time JSON snapshots."""

        account_ids = {candidate.remote_account_id for candidate in candidates}
        accounts = {
            account.id: account
            for account in (
                await self.db.execute(
                    select(RemoteAccount).where(
                        RemoteAccount.id.in_(account_ids),
                        RemoteAccount.user_id == self.user_id,
                    )
                )
            ).scalars().all()
        } if account_ids else {}
        result: list[DiscoveryCandidateRead] = []
        for candidate in candidates:
            if candidate.user_id != self.user_id or candidate.remote_account_id not in accounts:
                raise ValueError("Discovery candidate not found")
            account = accounts[candidate.remote_account_id]
            candidate_read = DiscoveryCandidateRead.model_validate(candidate)
            metadata = candidate.candidate_metadata or {}
            image_urls = metadata.get("profile_image_urls") or {}
            avatar = None
            if account.source == "pixiv" and isinstance(image_urls, Mapping):
                for key in ("medium", "square_medium", "large"):
                    upstream = image_urls.get(key)
                    if isinstance(upstream, str):
                        try:
                            avatar = self._media_url(candidate, account, upstream, "avatar")
                        except ValueError:
                            avatar = None
                        if avatar:
                            break
            recent: list[DiscoveryRecentWorkRead] = []
            raw_works = metadata.get("recent_works") or []
            if isinstance(raw_works, list):
                for raw in raw_works[:3]:
                    if not isinstance(raw, Mapping):
                        continue
                    try:
                        thumbnail = self._media_url(
                            candidate,
                            account,
                            str(raw["thumbnail_url"]) if raw.get("thumbnail_url") else None,
                            "thumbnail",
                        )
                        recent.append(
                            DiscoveryRecentWorkRead(
                                source_work_id=str(raw["source_work_id"]),
                                title=str(raw.get("title") or raw["source_work_id"]),
                                work_url=str(raw["work_url"]),
                                created_at=raw["created_at"],
                                work_type=raw["work_type"],
                                page_count=raw["page_count"],
                                x_restrict=raw["x_restrict"],
                                thumbnail_url=thumbnail,
                            )
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
            result.append(
                candidate_read.model_copy(
                    update={"avatar_url": avatar, "recent_works": recent}
                )
            )
        return result

    async def _provider_call(
        self,
        account: RemoteAccount,
        operation,
    ):
        account_service = RemoteAccountService(
            self.db,
            self.user_id,
            vault=self.vault or configured_credential_vault(),
            adapters=self.adapters,
        )
        account_id = account.id
        pinned_identity = account_service._credential_use_identity(account)
        pinned_generation = int(account.credential_generation or 0)
        credentials = account_service.credentials_for_adapter(
            account,
            expected_generation=pinned_generation,
        )
        try:
            result = await operation(self.adapters.get(account.source), credentials)
        except RemoteCredentialGenerationChanged:
            raise
        except Exception as exc:
            current = await account_service._relock_provider_result(
                account_id,
                pinned_identity=pinned_identity,
                pinned_generation=pinned_generation,
            )
            if isinstance(exc, RemoteReauthenticationRequired):
                current.auth_status = "unhealthy"
                current.auth_error_reason = "reauthentication_required"
                await account_service._set_binding_health(
                    current,
                    healthy=False,
                    failure_reason="reauthentication_required",
                )
                await self.db.flush()
            raise
        current = await account_service._relock_provider_result(
            account_id,
            pinned_identity=pinned_identity,
            pinned_generation=pinned_generation,
        )
        if not current.is_enabled or current.auth_status != "healthy":
            raise RemoteCredentialGenerationChanged(
                "remote account eligibility changed while provider request was running"
            )
        return result, current

    def _media_url(
        self,
        candidate: DiscoveryCandidate,
        account: RemoteAccount,
        upstream_url: str | None,
        variant: str,
    ) -> str | None:
        if not upstream_url:
            return None
        token = self.tokens.issue_media(
            user_id=self.user_id,
            candidate_id=candidate.id,
            remote_account_id=account.id,
            credential_generation=int(account.credential_generation),
            upstream_url=upstream_url,
            variant=variant,
        )
        return f"/api/v1/remote-media/{token}"

    async def _work_states(
        self,
        works: tuple[RemoteWorkPreview, ...],
    ) -> tuple[dict[str, UUID], dict[str, DownloadJob]]:
        ids = [work.source_work_id for work in works]
        if not ids:
            return {}, {}
        local_rows = (
            await self.db.execute(
                select(WorkSource).where(
                    WorkSource.source == "pixiv",
                    WorkSource.source_work_id.in_(ids),
                )
            )
        ).scalars().all()
        local = {row.source_work_id: row.work_id for row in local_rows}
        jobs = (
            await self.db.execute(
                select(DownloadJob)
                .where(
                    DownloadJob.owner_user_id == self.user_id,
                    DownloadJob.source == "pixiv",
                    DownloadJob.status.in_(ACTIVE_DOWNLOAD_STATUSES),
                    DownloadJob.manifest["source_work_id"].astext.in_(ids),
                )
                .order_by(DownloadJob.created_at.desc())
            )
        ).scalars().all()
        queued: dict[str, DownloadJob] = {}
        for job in jobs:
            source_work_id = str((job.manifest or {}).get("source_work_id") or "")
            queued.setdefault(source_work_id, job)
        return local, queued

    async def _page_read(
        self,
        candidate: DiscoveryCandidate,
        account: RemoteAccount,
        page: RemoteWorkPage,
    ) -> RemoteWorkPageRead:
        local, queued = await self._work_states(page.items)
        items: list[RemoteWorkPreviewRead] = []
        for work in page.items:
            local_work_id = local.get(work.source_work_id)
            job = queued.get(work.source_work_id)
            work_token = self.tokens.issue_work(
                {
                    "user_id": self.user_id,
                    "candidate_id": str(candidate.id),
                    "remote_account_id": str(account.id),
                    "credential_generation": int(account.credential_generation),
                    "source": account.source,
                    "source_creator_id": candidate.source_creator_id,
                    "source_work_id": work.source_work_id,
                    "work_url": work.work_url,
                    "x_restrict": work.x_restrict,
                }
            )
            items.append(
                RemoteWorkPreviewRead(
                    source_work_id=work.source_work_id,
                    source_creator_id=work.source_creator_id,
                    title=work.title,
                    work_url=work.work_url,
                    created_at=work.created_at,
                    work_type=work.work_type,
                    page_count=work.page_count,
                    x_restrict=work.x_restrict,
                    thumbnail_url=self._media_url(
                        candidate, account, work.thumbnail_url, "thumbnail"
                    ),
                    preview_urls=[
                        self._media_url(candidate, account, url, "preview")
                        for url in work.preview_urls
                    ],
                    local_work_id=local_work_id,
                    download_job_id=job.id if job else None,
                    import_status=(
                        "imported" if local_work_id else "queued" if job else "available"
                    ),
                    work_token=work_token,
                )
            )
        next_cursor = None
        if page.next_cursor is not None:
            next_cursor = self.tokens.issue_cursor(
                {
                    "user_id": self.user_id,
                    "candidate_id": str(candidate.id),
                    "remote_account_id": str(account.id),
                    "credential_generation": int(account.credential_generation),
                    "source_creator_id": candidate.source_creator_id,
                    "cursor": dict(page.next_cursor),
                }
            )
        return RemoteWorkPageRead(items=items, next_cursor=next_cursor)

    def _profile_read(
        self,
        candidate: DiscoveryCandidate,
        account: RemoteAccount,
        profile: RemoteCreatorProfile,
    ) -> RemoteCreatorProfileRead:
        return RemoteCreatorProfileRead(
            source=profile.source,
            source_creator_id=profile.source_creator_id,
            display_name=profile.display_name,
            username=profile.username,
            profile_url=profile.profile_url,
            avatar_url=self._media_url(candidate, account, profile.avatar_url, "avatar"),
            comment=profile.comment,
            work_counts=dict(profile.work_counts),
            is_followed=profile.is_followed,
            fetched_at=profile.fetched_at,
        )

    async def get_detail(self, candidate_id: UUID, *, limit: int = 20) -> RemoteCreatorDetailRead:
        candidate, account = await self._context(candidate_id)
        result, account = await self._provider_call(
            account,
            lambda adapter, credentials: adapter.fetch_creator_detail(
                credentials,
                source_creator_id=candidate.source_creator_id,
                page_size=limit,
            ),
        )
        if not isinstance(result, RemoteCreatorDetail):
            raise ValueError("Remote adapter returned an invalid creator detail")
        if result.profile.source_creator_id != candidate.source_creator_id:
            raise ValueError("Remote adapter returned a different creator")
        return RemoteCreatorDetailRead(
            profile=self._profile_read(candidate, account, result.profile),
            works=await self._page_read(candidate, account, result.works),
        )

    async def get_works(
        self,
        candidate_id: UUID,
        *,
        cursor: str,
        limit: int = 20,
    ) -> RemoteWorkPageRead:
        candidate, account = await self._context(candidate_id)
        try:
            cursor_payload = self.tokens.verify_cursor(cursor)
            if (
                int(cursor_payload["user_id"]) != self.user_id
                or cursor_payload["candidate_id"] != str(candidate.id)
                or cursor_payload["remote_account_id"] != str(account.id)
                or int(cursor_payload["credential_generation"])
                != int(account.credential_generation)
                or cursor_payload["source_creator_id"] != candidate.source_creator_id
                or not isinstance(cursor_payload["cursor"], Mapping)
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, RemoteAccessTokenError) as exc:
            raise RemoteAccessTokenError("remote works cursor is invalid") from exc
        result, account = await self._provider_call(
            account,
            lambda adapter, credentials: adapter.fetch_creator_works(
                credentials,
                source_creator_id=candidate.source_creator_id,
                cursor=dict(cursor_payload["cursor"]),
                page_size=limit,
            ),
        )
        if not isinstance(result, RemoteWorkPage) or any(
            item.source_creator_id != candidate.source_creator_id for item in result.items
        ):
            raise ValueError("Remote adapter returned works for a different creator")
        return await self._page_read(candidate, account, result)
