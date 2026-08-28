"""Download job orchestration — enqueue, manifest, task integration."""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UserSubscription, UserSubscriptionSource
from app.providers import registry
from app.services.backpressure import admission_error, download_backpressure_reason
from app.services.download_dispatch import prepare_download_dispatch, publish_prepared_download
from app.services.job_manifest import append_manifest_event, update_manifest
from app.services.job_progress import apply_download_progress

logger = logging.getLogger(__name__)

RQ_JOB_TIMEOUT = 7200


class DownloadOrchestrator:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _validate_caller_provenance(
        self,
        data: dict,
        *,
        user_id: int | None,
        subscription_source_id: UUID | None,
    ) -> tuple[UUID | None, UUID | None]:
        membership_id = data.get("triggering_user_subscription_id")
        account_id = data.get("triggering_remote_account_id")
        membership_id = UUID(str(membership_id)) if membership_id is not None else None
        account_id = UUID(str(account_id)) if account_id is not None else None
        if user_id is None:
            if membership_id is not None or account_id is not None:
                raise ValueError("Authenticated ownership context is required")
            return None, None
        if membership_id is None:
            raise ValueError("Authenticated membership ownership is required")

        membership = (
            await self.db.execute(
                select(UserSubscription).where(
                    UserSubscription.id == membership_id,
                    UserSubscription.user_id == user_id,
                    UserSubscription.subscription_id == data.get("subscription_id"),
                    UserSubscription.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            raise ValueError("Download ownership does not match the authenticated user")
        if subscription_source_id is None:
            if account_id is not None:
                raise ValueError("Remote account ownership requires a subscription source")
            return membership.id, None

        account_condition = (
            UserSubscriptionSource.remote_account_id == account_id
            if account_id is not None
            else UserSubscriptionSource.remote_account_id.is_(None)
        )
        binding = (
            await self.db.execute(
                select(UserSubscriptionSource).where(
                    UserSubscriptionSource.user_subscription_id == membership.id,
                    UserSubscriptionSource.subscription_source_id == subscription_source_id,
                    UserSubscriptionSource.user_id == user_id,
                    account_condition,
                )
            )
        ).scalar_one_or_none()
        if binding is None:
            raise ValueError("Download ownership does not match the requested source")
        return membership.id, binding.remote_account_id

    async def create(self, data: dict, repo, *, user_id: int | None = None) -> dict:
        source = data.get("source", "")
        source_url = data.get("source_url", "")
        subscription_source_id = data.get("subscription_source_id")

        ss_uuid = None
        if subscription_source_id:
            ss_uuid = subscription_source_id if isinstance(subscription_source_id, UUID) else UUID(subscription_source_id) if subscription_source_id else None
        membership_id, account_id = await self._validate_caller_provenance(
            data,
            user_id=user_id,
            subscription_source_id=ss_uuid,
        )
        if ss_uuid:
            from app.services.subscription_enqueue import enqueue_subscription_source_sync
            result = await enqueue_subscription_source_sync(
                self.db,
                ss_uuid,
                trigger="manual_source",
                triggering_user_subscription_id=membership_id,
                triggering_remote_account_id=account_id,
            )
            if result["status"] == "enqueued":
                return {"job_id": result["job_id"], "status": "enqueued", "source_url": result["source_url"]}
            if result.get("job_id") and result.get("skip_reason") == "already_running":
                running = await repo.get(UUID(result["job_id"]))
                return {
                    "job_id": result["job_id"],
                    "status": running.status if running else "pending",
                    "source_url": running.source_url if running else source_url,
                }
            reason = result.get("reason") or {}
            code = result.get("skip_reason") or reason.get("code")
            if code in {
                "queue_saturated",
                "enqueue_busy",
                "redis_capacity",
                "redis_unwritable",
                "disk_backpressure",
                "import_backpressure",
                "storage_unavailable",
                "admission_check_failed",
                "enqueue_failed",
            }:
                raise admission_error({"code": code, **reason.get("details", {}), "message": reason.get("message")})
            raise ValueError(result.get("skip_reason") or result.get("error") or "Unable to enqueue source sync")

        if not source_url:
            raise ValueError("source_url is required")

        try:
            provider = registry.get(source)
        except KeyError:
            raise ValueError(f"Unknown source provider: {source}")
        normalized_url = provider.normalize_url(source_url) or source_url
        if not provider.validate_url(normalized_url):
            raise ValueError(f"Invalid URL for source '{source}': {source_url}")

        pressure = await download_backpressure_reason(
            self.db,
            automatic=False,
            include_queue=True,
        )
        if pressure:
            raise admission_error(pressure)

        job = await repo.create({
            "subscription_id": data["subscription_id"],
            "subscription_source_id": subscription_source_id,
            "source": source,
            "source_url": normalized_url,
            "status": "enqueued",
            "triggering_user_subscription_id": membership_id,
            "triggering_remote_account_id": account_id,
            "owner_user_id": user_id,
        })
        apply_download_progress(job, "enqueued", "Queued; waiting for download worker", publish=False)
        update_manifest(job, trigger="manual_url", source=source, source_url=normalized_url)
        append_manifest_event(job, "created", trigger="manual_url")
        prepared = await prepare_download_dispatch(
            self.db,
            job,
            queue_name="downloads",
            job_timeout=RQ_JOB_TIMEOUT,
            action="manual_url",
        )

        try:
            await publish_prepared_download(
                self.db,
                job,
                prepared,
                job_timeout=RQ_JOB_TIMEOUT,
                action="manual_url",
            )
        except Exception:
            logger.error("Failed to enqueue download job %s", job.id, exc_info=True)
            raise

        apply_download_progress(
            job,
            "enqueued",
            "Queued; waiting for download worker",
            publish=False,
        )
        try:
            from app.services.job_progress import publish_progress

            publish_progress(str(job.id), "download", job.progress_data)
        except Exception:
            # The durable DB row and RQ job already exist; an ephemeral pub/sub
            # failure must not turn a valid queued job into an API failure.
            logger.warning("Failed to publish queued progress for %s", job.id, exc_info=True)

        return {"job_id": str(job.id), "status": job.status, "source_url": normalized_url}
