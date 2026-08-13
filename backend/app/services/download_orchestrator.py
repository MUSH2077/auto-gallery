"""Download job orchestration — enqueue, manifest, task integration."""

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

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

    async def create(self, data: dict, repo) -> dict:
        source = data.get("source", "")
        source_url = data.get("source_url", "")
        subscription_source_id = data.get("subscription_source_id")

        ss_uuid = None
        if subscription_source_id:
            ss_uuid = subscription_source_id if isinstance(subscription_source_id, UUID) else UUID(subscription_source_id) if subscription_source_id else None
        if ss_uuid:
            from app.services.subscription_enqueue import enqueue_subscription_source_sync
            result = await enqueue_subscription_source_sync(self.db, ss_uuid, trigger="manual_source")
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
