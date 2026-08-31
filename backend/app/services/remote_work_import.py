"""Idempotent exact-work imports using the existing download pipeline."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DiscoveryCandidate,
    DownloadJob,
    RemoteAccount,
    UserSubscriptionSource,
    WorkSource,
)
from app.models.task_state import DOWNLOAD_RUNNING_STATUSES
from app.remote_discovery.registry import DiscoveryAdapterRegistry, registry
from app.repositories.download_job import DownloadJobRepository
from app.schemas.remote_discovery import RemoteWorkImportRead
from app.services.backpressure import admission_error, download_backpressure_reason
from app.services.download_dispatch import prepare_download_dispatch, publish_prepared_download
from app.services.job_manifest import append_manifest_event, update_manifest
from app.services.job_progress import apply_download_progress
from app.services.remote_access_tokens import RemoteAccessTokenError, RemoteAccessTokenService
from app.services.remote_creator_access import RemoteCreatorAccessService
from app.services.remote_credentials import CredentialVault
from app.services.remote_discovery import RemoteDiscoveryService
from app.services.remote_accounts import configured_credential_vault


RQ_JOB_TIMEOUT = 7200


class RemoteWorkSourceBusy(RuntimeError):
    """The canonical creator source already has a different active download."""


class RemoteSensitiveConfirmationRequired(ValueError):
    """Sensitive remote work has not been explicitly revealed by the user."""


class RemoteWorkImportService:
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
        self.vault = vault or configured_credential_vault()
        self.adapters = adapters or registry
        self.tokens = tokens or RemoteAccessTokenService()

    async def _validate_ticket(
        self,
        candidate_id: UUID,
        work_token: str,
        *,
        sensitive_content_confirmed: bool,
    ) -> tuple[dict, DiscoveryCandidate, RemoteAccount]:
        try:
            payload = self.tokens.verify_work(work_token)
            if (
                int(payload["user_id"]) != self.user_id
                or payload["candidate_id"] != str(candidate_id)
                or payload["source"] != "pixiv"
                or not str(payload["source_work_id"]).strip()
                or payload["work_url"]
                != f"https://www.pixiv.net/artworks/{payload['source_work_id']}"
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, RemoteAccessTokenError) as exc:
            raise RemoteAccessTokenError("remote work token is invalid") from exc
        access = RemoteCreatorAccessService(
            self.db,
            self.user_id,
            vault=self.vault,
            adapters=self.adapters,
            tokens=self.tokens,
        )
        candidate, account = await access._context(candidate_id)
        if (
            payload["remote_account_id"] != str(account.id)
            or int(payload["credential_generation"])
            != int(account.credential_generation)
            or payload["source_creator_id"] != candidate.source_creator_id
        ):
            raise RemoteAccessTokenError("remote work token is invalid")
        if int(payload.get("x_restrict", 0)) > 0 and not sensitive_content_confirmed:
            raise RemoteSensitiveConfirmationRequired(
                "Sensitive remote work must be revealed before import"
            )
        if candidate.state == "dismissed":
            raise ValueError("Dismissed candidate must be restored before import")
        if candidate.state == "conflict" or (candidate.candidate_metadata or {}).get(
            "identity_conflict"
        ):
            raise ValueError("Discovery candidate has an unresolved conflict")
        return payload, candidate, account

    async def _existing_local(self, source_work_id: str) -> UUID | None:
        return (
            await self.db.execute(
                select(WorkSource.work_id).where(
                    WorkSource.source == "pixiv",
                    WorkSource.source_work_id == source_work_id,
                )
            )
        ).scalar_one_or_none()

    async def _active_job(self, source_id: UUID) -> DownloadJob | None:
        return (
            await self.db.execute(
                select(DownloadJob)
                .where(
                    DownloadJob.subscription_source_id == source_id,
                    DownloadJob.status.in_(DOWNLOAD_RUNNING_STATUSES),
                )
                .order_by(DownloadJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def import_work(
        self,
        candidate_id: UUID,
        work_token: str,
        *,
        sensitive_content_confirmed: bool,
    ) -> RemoteWorkImportRead:
        payload, candidate, account = await self._validate_ticket(
            candidate_id,
            work_token,
            sensitive_content_confirmed=sensitive_content_confirmed,
        )
        source_work_id = str(payload["source_work_id"])
        local_work_id = await self._existing_local(source_work_id)
        if local_work_id is not None:
            return RemoteWorkImportRead(
                status="already_imported",
                local_work_id=local_work_id,
            )

        if candidate.state != "imported":
            candidate = await RemoteDiscoveryService(
                self.db,
                vault=self.vault,
                adapters=self.adapters,
            ).import_candidate(
                self.user_id,
                candidate_id,
                manual_new_membership=True,
            )
            account = await self.db.get(RemoteAccount, candidate.remote_account_id)

        binding = (
            await self.db.execute(
                select(UserSubscriptionSource).where(
                    UserSubscriptionSource.user_subscription_id
                    == candidate.user_subscription_id,
                    UserSubscriptionSource.user_id == self.user_id,
                    UserSubscriptionSource.remote_account_id == account.id,
                )
            )
        ).scalar_one_or_none()
        if binding is None:
            raise ValueError("Imported candidate source binding not found")

        active = await self._active_job(binding.subscription_source_id)
        if active is not None:
            if str((active.manifest or {}).get("source_work_id") or "") == source_work_id:
                return RemoteWorkImportRead(
                    status="already_queued",
                    download_job_id=active.id,
                )
            raise RemoteWorkSourceBusy("Another download is active for this source")

        pressure = await download_backpressure_reason(
            self.db,
            automatic=False,
            include_queue=True,
        )
        if pressure:
            raise admission_error(pressure)

        repo = DownloadJobRepository(self.db)
        try:
            async with self.db.begin_nested():
                job = await repo.create(
                    {
                        "subscription_id": candidate.subscription_id,
                        "subscription_source_id": binding.subscription_source_id,
                        "source": "pixiv",
                        "source_url": str(payload["work_url"]),
                        "status": "enqueued",
                        "triggering_user_subscription_id": candidate.user_subscription_id,
                        "triggering_remote_account_id": account.id,
                        "triggering_credential_generation": account.credential_generation,
                        "owner_user_id": self.user_id,
                    }
                )
                apply_download_progress(
                    job,
                    "enqueued",
                    "Queued; waiting for download worker",
                    publish=False,
                )
                update_manifest(
                    job,
                    trigger="remote_work_import",
                    source="pixiv",
                    source_url=str(payload["work_url"]),
                    source_work_id=source_work_id,
                    source_creator_id=candidate.source_creator_id,
                    candidate_id=str(candidate.id),
                    subscription_source_id=str(binding.subscription_source_id),
                    subscription_id=str(candidate.subscription_id),
                )
                append_manifest_event(job, "created", trigger="remote_work_import")
                await self.db.flush()
        except IntegrityError:
            active = await self._active_job(binding.subscription_source_id)
            if active is not None and str(
                (active.manifest or {}).get("source_work_id") or ""
            ) == source_work_id:
                return RemoteWorkImportRead(
                    status="already_queued",
                    download_job_id=active.id,
                )
            raise RemoteWorkSourceBusy("Another download is active for this source")

        prepared = await prepare_download_dispatch(
            self.db,
            job,
            queue_name="downloads",
            job_timeout=RQ_JOB_TIMEOUT,
            action="remote_work_import",
        )
        await publish_prepared_download(
            self.db,
            job,
            prepared,
            job_timeout=RQ_JOB_TIMEOUT,
            action="remote_work_import",
        )
        return RemoteWorkImportRead(status="queued", download_job_id=job.id)
