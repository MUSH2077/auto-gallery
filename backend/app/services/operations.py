"""Shared status storage for background admin operations."""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from uuid import UUID, uuid4

import redis as redis_lib
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import DateTime, cast, func as sql_func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.redis_client import get_redis
from app.services.queue_admission import QueueAdmissionError, checked_enqueue

# Long adaptive rebuilds can legitimately spend several days yielding at the
# 10% budget floor.  Their single-flight/status records must outlive the RQ
# timeout or a second writer could be admitted while the first is still alive.
OPERATION_TTL_SECONDS = 8 * 24 * 60 * 60
logger = logging.getLogger(__name__)

ADMIN_DISPATCH_META_KEY = "admin_dispatch"
ADMIN_DISPATCH_PENDING = "pending"
ADMIN_DISPATCH_PUBLISHED = "published"
ADMIN_DISPATCH_FAILED = "failed"
ADMIN_DISPATCH_RECOVERY_INTERVAL_SECONDS = 30
ADMIN_ATTEMPT_HEARTBEAT_INTERVAL_SECONDS = 10
ADMIN_ATTEMPT_HEARTBEAT_STALE_SECONDS = 90
ADMIN_DISPATCH_RECOVERY_LIMIT = 25
ADMIN_DISPATCH_GRACE_SECONDS = 15
ADMIN_DISPATCH_RETRY_MIN_SECONDS = 30
ADMIN_DISPATCH_RETRY_MAX_SECONDS = 15 * 60
ADMIN_RQ_FUNCTION = "app.jobs.admin_operations.run_registered_admin_operation"
_ACTIVE_ADMIN_STATUSES = frozenset({"enqueued", "running", "paused", "recovering"})
_ADMIN_INTERNAL_RESOURCE_PROFILES = {
    "admin-clear": "maintenance",
    "admin-rebuild": "maintenance",
    "admin-disk-import": "maintenance",
    "admin-creator-reenrich": "maintenance",
    "danbooru-mapping-refresh": "maintenance",
    "admin-search-reindex": "search_index",
    "admin-curation-backfill": "import_db",
    "admin-gitllery-sync": "git_projection",
    "hierarchy-delete": "maintenance",
    "asset-dedup-scan": "image_derive",
    "admin-integrity-scan": "maintenance",
    "admin-backup-estimate": "maintenance",
    "admin-backup-create": "maintenance",
    "admin-restore-validate": "maintenance",
    "admin-proxy-test": "maintenance",
    "admin-gallerydl-connectivity-test": "maintenance",
}
_ADMIN_OPERATION_ATTEMPT: ContextVar[tuple[UUID, int] | None] = ContextVar(
    "admin_operation_attempt",
    default=None,
)


class AdminOperationAttemptRejected(RuntimeError):
    """A worker callback no longer owns the current durable attempt."""


@contextmanager
def admin_operation_attempt_context(task_id: UUID | str, attempt: int):
    """Propagate one claimed delivery token through nested worker callbacks."""

    token = _ADMIN_OPERATION_ATTEMPT.set((UUID(str(task_id)), int(attempt)))
    try:
        yield
    finally:
        _ADMIN_OPERATION_ATTEMPT.reset(token)


def current_admin_operation_attempt() -> tuple[UUID, int] | None:
    """Return the registered worker delivery active in this async context."""

    return _ADMIN_OPERATION_ATTEMPT.get()


@dataclass(frozen=True)
class AdminOperationSpec:
    operation_type: str
    legacy_function: str
    queue_names: frozenset[str]
    scope_prefixes: tuple[str, ...]
    default_job_timeout: int
    required_permission: str


@dataclass(frozen=True)
class PreparedAdminDispatch:
    task: Any
    rq_job_id: str
    attempt: int
    queue_name: str
    job_timeout: int


def _spec(
    operation_type: str,
    legacy_function: str,
    *,
    queues: tuple[str, ...] = ("maintenance",),
    scopes: tuple[str, ...],
    timeout: int = 14400,
    permission: str = "system",
) -> AdminOperationSpec:
    return AdminOperationSpec(
        operation_type=operation_type,
        legacy_function=legacy_function,
        queue_names=frozenset(queues),
        scope_prefixes=scopes,
        default_job_timeout=timeout,
        required_permission=permission,
    )


ADMIN_OPERATION_REGISTRY: dict[str, AdminOperationSpec] = {
    spec.operation_type: spec
    for spec in (
        _spec(
            "admin-clear",
            "app.jobs.admin_operations.run_clear_operation",
            scopes=("library:clear:",),
            timeout=7200,
        ),
        _spec(
            "admin-cleanup-metadata-jsons",
            "app.jobs.admin_operations.run_cleanup_metadata_jsons_operation",
            scopes=("library:cleanup-metadata-jsons:active",),
            timeout=7200,
        ),
        _spec(
            "admin-rebuild",
            "app.jobs.admin_operations.run_library_rebuild_operation",
            scopes=("library:rebuild:active",),
        ),
        _spec(
            "admin-disk-import",
            "app.jobs.admin_operations.run_disk_import_operation",
            scopes=("library:disk-import:active",),
        ),
        _spec(
            "admin-creator-reenrich",
            "app.jobs.admin_operations.run_creator_reenrich_operation",
            scopes=("library:creator-reenrich:active",),
            timeout=7200,
        ),
        _spec(
            "danbooru-mapping-refresh",
            "app.jobs.admin_operations.run_creator_reenrich_operation",
            scopes=("library:creator-reenrich:active",),
            permission="subscriptions",
        ),
        _spec(
            "admin-danbooru-batch-import",
            "app.jobs.batch_import.run_batch_import",
            queues=("imports",),
            scopes=("danbooru:batch-import:",),
            timeout=3600,
            permission="subscriptions",
        ),
        _spec(
            "danbooru-import-all",
            "app.jobs.danbooru_import.run_import_all_danbooru",
            queues=("imports",),
            scopes=("danbooru:import-all:",),
            timeout=3600,
            permission="subscriptions",
        ),
        _spec(
            "admin-danbooru-url-batch-import",
            "app.jobs.batch_import.run_url_batch_import",
            queues=("imports",),
            scopes=("danbooru:url-batch-import:",),
            timeout=3600,
            permission="subscriptions",
        ),
        _spec(
            "admin-search-reindex",
            "app.jobs.admin_operations.run_search_reindex_operation",
            scopes=("library:search-reindex:active",),
            timeout=7 * 24 * 60 * 60,
        ),
        _spec(
            "admin-curation-backfill",
            "app.jobs.admin_operations.run_curation_backfill_operation",
            scopes=("library:curation-backfill:active",),
            timeout=7 * 24 * 60 * 60,
            permission="curation",
        ),
        _spec(
            "admin-gitllery-verify",
            "app.jobs.admin_operations.run_gitllery_verify_operation",
            scopes=("gitllery:verify:",),
            timeout=7 * 24 * 60 * 60,
            permission="curation",
        ),
        _spec(
            "admin-gitllery-sync",
            "app.jobs.admin_operations.run_gitllery_sync_operation",
            scopes=("library:gitllery-sync:active",),
            timeout=7 * 24 * 60 * 60,
        ),
        _spec(
            "hierarchy-delete",
            "app.jobs.admin_operations.run_hierarchy_delete_operation",
            queues=("operations", "maintenance"),
            scopes=("library:hierarchy-delete:active",),
            timeout=7 * 24 * 60 * 60,
        ),
        _spec(
            "asset-dedup-scan",
            "app.jobs.asset_dedup.run_asset_dedup_scan",
            scopes=("lock:admin:asset-dedup-scan",),
            timeout=3600,
            permission="curation",
        ),
        _spec(
            "admin-download-conflict-reconciliation",
            "app.jobs.download_conflicts.reconcile_historical_download_conflicts",
            scopes=("diagnostics:download-conflicts:active",),
            timeout=3600,
        ),
        _spec(
            "admin-integrity-scan",
            "app.jobs.admin_operations.run_integrity_scan_operation",
            scopes=("diagnostics:integrity:active",),
        ),
        _spec(
            "admin-backup-estimate",
            "app.jobs.admin_operations.run_backup_estimate_operation",
            scopes=("backup:estimate:active",),
        ),
        _spec(
            "admin-backup-create",
            "app.jobs.admin_operations.run_backup_create_operation",
            scopes=("backup:create:active",),
            timeout=3600,
        ),
        _spec(
            "admin-restore-validate",
            "app.jobs.admin_operations.run_restore_validation_operation",
            scopes=("restore:validate:",),
            timeout=3600,
        ),
        _spec(
            "admin-proxy-test",
            "app.jobs.admin_operations.run_proxy_test_operation",
            scopes=("diagnostics:proxy:active",),
            timeout=120,
        ),
        _spec(
            "admin-gallerydl-connectivity-test",
            "app.jobs.admin_operations.run_gallerydl_connectivity_test_operation",
            scopes=("diagnostics:gallerydl:",),
            timeout=180,
        ),
    )
}


def admin_operation_required_permission(operation_type: str | None) -> str | None:
    """Return the owning module for a closed-registry administrator operation."""

    spec = ADMIN_OPERATION_REGISTRY.get(str(operation_type or ""))
    return spec.required_permission if spec is not None else None


def can_access_admin_operation(user: Any, operation_type: str | None) -> bool:
    """Keep generic task surfaces from crossing an operation's module boundary."""

    required = admin_operation_required_permission(operation_type)
    if required is None or bool(getattr(user, "is_admin", False)):
        return True
    return required in set(getattr(user, "permissions", None) or ())


def admin_operation_permissions_for_user(user: Any) -> frozenset[str]:
    """Return operation-owner permissions, expanding administrator access."""

    if bool(getattr(user, "is_admin", False)):
        return frozenset(
            spec.required_permission for spec in ADMIN_OPERATION_REGISTRY.values()
        )
    return frozenset(getattr(user, "permissions", None) or ())


def inaccessible_admin_operation_types_for_permissions(
    permissions: Iterable[str],
) -> frozenset[str]:
    """Derive hidden registered operations from an explicit permission set."""

    permission_set = set(permissions)
    return frozenset(
        operation_type
        for operation_type, spec in ADMIN_OPERATION_REGISTRY.items()
        if spec.required_permission not in permission_set
    )


def inaccessible_admin_operation_types(user: Any) -> frozenset[str]:
    """List registered operation types hidden from this authenticated user."""

    return inaccessible_admin_operation_types_for_permissions(
        admin_operation_permissions_for_user(user)
    )


def require_admin_operation_access(user: Any, operation_type: str | None) -> None:
    """Reject generic detail/control access without exposing stored task data."""

    required = admin_operation_required_permission(operation_type)
    if required is not None and not can_access_admin_operation(user, operation_type):
        raise HTTPException(status_code=403, detail=f"Missing permission: {required}")


_ACQUIRE_ATTEMPT_OPERATION_SCRIPT = """
local current = redis.call('get', KEYS[1])
local current_attempt = redis.call('get', KEYS[2])
if current then
  if current ~= ARGV[1] then
    return 0
  end
  if ARGV[4] ~= '1' then
    return 0
  end
end
if ARGV[5] == '1' then
  if current_attempt then
    return 0
  end
elseif current_attempt ~= ARGV[6] then
  return 0
end
redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[3])
redis.call('set', KEYS[2], ARGV[2], 'EX', ARGV[3])
return 1
"""

_ACQUIRE_LEGACY_OPERATION_SCRIPT = """
if redis.call('exists', KEYS[2]) == 1 or redis.call('exists', KEYS[3]) == 1 then
  return 0
end
return redis.call('set', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) and 1 or 0
"""

_RELEASE_ATTEMPT_OPERATION_SCRIPT = """
local current = redis.call('get', KEYS[1])
local attempt = redis.call('get', KEYS[2])
if current == ARGV[1] and attempt == ARGV[2] then
  return redis.call('del', KEYS[1])
end
return 0
"""

_RELEASE_LEGACY_OPERATION_SCRIPT = """
if redis.call('exists', KEYS[2]) == 1 or redis.call('exists', KEYS[3]) == 1 then
  return 0
end
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""

_SET_LEGACY_OPERATION_STATUS_SCRIPT = """
if redis.call('exists', KEYS[1]) == 1 or redis.call('exists', KEYS[2]) == 1 then
  return 0
end
redis.call('setex', KEYS[3], ARGV[1], ARGV[2])
return 1
"""

_SET_ATTEMPT_OPERATION_STATUS_SCRIPT = """
if redis.call('get', KEYS[1]) ~= ARGV[1] then
  return 0
end
redis.call('setex', KEYS[2], ARGV[2], ARGV[3])
redis.call('setex', KEYS[3], ARGV[2], ARGV[1])
redis.call('expire', KEYS[1], ARGV[2])
return 1
"""

_GET_CURRENT_OPERATION_STATUS_SCRIPT = """
local attempt = redis.call('get', KEYS[1])
local cache_attempt = redis.call('get', KEYS[2])
if attempt then
  if not cache_attempt or cache_attempt ~= attempt then
    return false
  end
elseif cache_attempt then
  return false
end
return redis.call('get', KEYS[3])
"""


def operation_key(job_id: str) -> str:
    return f"admin_operation:{job_id}"


def operation_attempt_key(job_id: str) -> str:
    """Private current-attempt pointer for one logical admin TaskRun."""

    return f"admin_operation_owner:{job_id}"


def operation_cache_attempt_key(job_id: str) -> str:
    """Private attempt version attached to the public cache projection."""

    return f"admin_operation_cache_owner:{job_id}"


def _decoded(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


def current_operation_attempt(redis, job_id: str) -> str | None:
    raw = _decoded(redis.get(operation_attempt_key(job_id)))
    return raw if isinstance(raw, str) and raw else None


async def durable_operation_attempt_is_terminal(
    job_id: str,
    publisher_attempt: str,
) -> bool:
    """Return terminal evidence only for the exact durable disk attempt.

    The Redis pointer remains the release CAS. This database read lets callers
    distinguish a valid first-cache publication gap from a genuinely terminal
    owner without holding a database row lock across Redis I/O.
    """

    from uuid import UUID

    from app.database import async_session
    from app.models.task_run import TaskRun
    from app.services.publisher_attempts import current_publisher_attempt

    try:
        task_id = UUID(job_id)
    except (TypeError, ValueError):
        return False
    async with async_session() as db:
        task = await db.get(TaskRun, task_id)
        return bool(
            task is not None
            and task.kind == "admin"
            and task.operation_type == "admin-disk-import"
            and current_publisher_attempt(task) == publisher_attempt
            and task.status in {"complete", "failed", "stale", "cancelled"}
        )


def acquire_operation_lock(
    redis,
    lock_key: str,
    job_id: str,
    *,
    ttl_seconds: int,
    publisher_attempt: str | None = None,
    replace_same_job: bool = False,
    expected_current_attempt: str | None = None,
) -> bool:
    """Acquire one single-flight owner, optionally scoped to an attempt.

    Attempt rotation may replace only the same logical TaskRun. An independent
    job remains a conflict, and the private attempt never enters the lock value
    returned by public conflict responses.
    """

    eval_script = getattr(redis, "eval", None)
    if publisher_attempt is None:
        if callable(eval_script):
            return bool(redis.eval(
                _ACQUIRE_LEGACY_OPERATION_SCRIPT,
                3,
                lock_key,
                operation_attempt_key(job_id),
                operation_cache_attempt_key(job_id),
                job_id,
                int(ttl_seconds),
            ))
        if current_operation_attempt(redis, job_id) is not None or _decoded(
            redis.get(operation_cache_attempt_key(job_id))
        ) is not None:
            return False
        return bool(redis.set(lock_key, job_id, nx=True, ex=ttl_seconds))
    if callable(eval_script):
        return bool(redis.eval(
            _ACQUIRE_ATTEMPT_OPERATION_SCRIPT,
            2,
            lock_key,
            operation_attempt_key(job_id),
            job_id,
            publisher_attempt,
            int(ttl_seconds),
            "1" if replace_same_job else "0",
            "1" if expected_current_attempt is None else "0",
            expected_current_attempt or "",
        ))
    # Compatibility for simple test/rolling clients with no EVAL method.
    # An actual EVAL failure must propagate fail-closed; check-then-write is
    # never a production fallback for immutable-attempt authority.
    current = _decoded(redis.get(lock_key))
    current_attempt = current_operation_attempt(redis, job_id)
    if current is not None and (
        current != job_id or not replace_same_job
    ):
        return False
    if current_attempt != expected_current_attempt:
        return False
    if not redis.set(lock_key, job_id, ex=int(ttl_seconds)):
        return False
    redis.set(
        operation_attempt_key(job_id),
        publisher_attempt,
        ex=int(ttl_seconds),
    )
    return True


def set_operation_status(
    job_id: str,
    status: str,
    operation_type: str,
    *,
    progress: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    meta: dict[str, Any] | None = None,
    publisher_attempt: str | None = None,
    redis_client=None,
) -> dict[str, Any] | None:
    # Registered operations project status from PostgreSQL.  Their legacy
    # business handlers still call this compatibility writer, so make those
    # calls inert rather than reintroducing Redis as a competing projection.
    if current_admin_operation_attempt() is not None:
        return None
    payload: dict[str, Any] = {
        "job_id": job_id,
        "status": status,
        "operation_type": operation_type,
        "updated_at": time.time(),
    }
    if progress is not None:
        payload["progress"] = progress
    if result is not None:
        payload["result"] = result
    if error is not None:
        payload["error"] = error
    if meta is not None:
        payload["meta"] = meta

    redis = redis_client if redis_client is not None else get_redis()
    encoded = json.dumps(payload, default=str)
    eval_script = getattr(redis, "eval", None)
    if publisher_attempt is None:
        if callable(eval_script):
            written = bool(redis.eval(
                _SET_LEGACY_OPERATION_STATUS_SCRIPT,
                3,
                operation_attempt_key(job_id),
                operation_cache_attempt_key(job_id),
                operation_key(job_id),
                OPERATION_TTL_SECONDS,
                encoded,
            ))
        else:
            if current_operation_attempt(redis, job_id) is not None or _decoded(
                redis.get(operation_cache_attempt_key(job_id))
            ) is not None:
                return None
            redis.setex(
                operation_key(job_id),
                OPERATION_TTL_SECONDS,
                encoded,
            )
            written = True
        return payload if written else None
    if callable(eval_script):
        written = bool(redis.eval(
            _SET_ATTEMPT_OPERATION_STATUS_SCRIPT,
            3,
            operation_attempt_key(job_id),
            operation_key(job_id),
            operation_cache_attempt_key(job_id),
            publisher_attempt,
            OPERATION_TTL_SECONDS,
            encoded,
        ))
    else:
        if current_operation_attempt(redis, job_id) != publisher_attempt:
            return None
        redis.setex(
            operation_key(job_id),
            OPERATION_TTL_SECONDS,
            encoded,
        )
        redis.setex(
            operation_cache_attempt_key(job_id),
            OPERATION_TTL_SECONDS,
            publisher_attempt,
        )
        written = True
    if not written:
        return None
    return payload


def get_operation_status(
    job_id: str,
    *,
    redis_client=None,
) -> dict[str, Any] | None:
    redis = redis_client if redis_client is not None else get_redis()
    eval_script = getattr(redis, "eval", None)
    if callable(eval_script):
        raw = redis.eval(
            _GET_CURRENT_OPERATION_STATUS_SCRIPT,
            3,
            operation_attempt_key(job_id),
            operation_cache_attempt_key(job_id),
            operation_key(job_id),
        )
    else:
        attempt = current_operation_attempt(redis, job_id)
        cache_attempt = _decoded(redis.get(operation_cache_attempt_key(job_id)))
        if (attempt and cache_attempt != attempt) or (
            not attempt and cache_attempt
        ):
            return None
        raw = redis.get(operation_key(job_id))
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def release_owned_operation_lock(
    redis,
    lock_key: str,
    job_id: str,
    *,
    publisher_attempt: str | None = None,
) -> bool:
    """Delete a single-flight key only while it still belongs to ``job_id``."""

    if publisher_attempt is not None:
        eval_script = getattr(redis, "eval", None)
        if callable(eval_script):
            return bool(redis.eval(
                _RELEASE_ATTEMPT_OPERATION_SCRIPT,
                2,
                lock_key,
                operation_attempt_key(job_id),
                job_id,
                publisher_attempt,
            ))
        current = _decoded(redis.get(lock_key))
        attempt = current_operation_attempt(redis, job_id)
        if current != job_id or attempt != publisher_attempt:
            return False
        return bool(redis.delete(lock_key))

    eval_script = getattr(redis, "eval", None)
    if callable(eval_script):
        return bool(redis.eval(
            _RELEASE_LEGACY_OPERATION_SCRIPT,
            3,
            lock_key,
            operation_attempt_key(job_id),
            operation_cache_attempt_key(job_id),
            job_id,
        ))
    # Deliberate compatibility for minimal in-memory clients that have no Lua
    # executor. Production Redis errors propagate instead of degrading to a
    # check/delete race.
    if current_operation_attempt(redis, job_id) is not None or _decoded(
        redis.get(operation_cache_attempt_key(job_id))
    ) is not None:
        return False
    current = _decoded(redis.get(lock_key))
    if current != job_id:
        return False
    return bool(redis.delete(lock_key))


def release_legacy_operation_lock(
    lock_key: str,
    job_id: str,
    *,
    publisher_attempt: str | None = None,
) -> bool:
    """Best-effort rolling cleanup which never runs for registered workers."""

    if current_admin_operation_attempt() is not None:
        return False
    try:
        return release_owned_operation_lock(
            get_redis(),
            lock_key,
            job_id,
            publisher_attempt=publisher_attempt,
        )
    except Exception:
        logger.warning(
            "Unable to release legacy operation lock key=%s job=%s",
            lock_key,
            job_id,
            exc_info=True,
        )
        return False


async def compensate_operation_enqueue_failure(
    job_id: str,
    operation_type: str,
    error: BaseException | str,
    *,
    lock_key: str | None = None,
    redis_client=None,
    publisher_attempt: str | None = None,
) -> None:
    """Make a committed admin operation terminal when RQ publication fails."""

    from app.services.publisher_attempts import redact_publisher_attempt

    message = "Queue publication failed: " + redact_publisher_attempt(
        error,
        publisher_attempt,
    )
    redis = redis_client if redis_client is not None else get_redis()

    async def compensate_task_run() -> bool:
        try:
            from uuid import UUID

            from app.database import async_session
            from app.services.publisher_attempts import (
                current_publisher_attempt,
                lock_publisher_task,
            )
            from app.services.tasks import TaskService

            async with async_session() as task_db:
                service = TaskService(task_db)
                task = (
                    await lock_publisher_task(task_db, UUID(job_id))
                    if publisher_attempt is not None
                    else await service.get(UUID(job_id))
                )
                if publisher_attempt is not None and (
                    task is None
                    or current_publisher_attempt(task) != publisher_attempt
                ):
                    await task_db.rollback()
                    return False
                if task is not None:
                    await service.update_task(
                        task,
                        status="failed",
                        progress={"phase": "failed", "label": message},
                        error=message,
                    )
                    await task_db.commit()
            return True
        except Exception:
            logger.exception(
                "Unable to compensate failed operation TaskRun job=%s",
                job_id,
            )
            return False

    # For immutable publishers the durable row is the authority boundary for
    # every compensating write, including the transient lock/status records.
    if publisher_attempt is not None:
        if not await compensate_task_run():
            return
        try:
            set_operation_status(
                job_id,
                "failed",
                operation_type,
                progress={"phase": "failed", "label": message},
                error=message,
                publisher_attempt=publisher_attempt,
                redis_client=redis,
            )
        except Exception:
            logger.warning(
                "Unable to persist failed operation Redis status job=%s",
                job_id,
                exc_info=True,
            )
        if lock_key:
            try:
                release_owned_operation_lock(
                    redis,
                    lock_key,
                    job_id,
                    publisher_attempt=publisher_attempt,
                )
            except Exception:
                logger.warning(
                    "Unable to release failed operation lock key=%s job=%s",
                    lock_key,
                    job_id,
                    exc_info=True,
                )
        return

    if lock_key:
        try:
            release_owned_operation_lock(redis, lock_key, job_id)
        except Exception:
            logger.warning(
                "Unable to release failed operation lock key=%s job=%s",
                lock_key,
                job_id,
                exc_info=True,
            )
    try:
        set_operation_status(
            job_id,
            "failed",
            operation_type,
            progress={"phase": "failed", "label": message},
            error=message,
        )
    except Exception:
        logger.warning(
            "Unable to persist failed operation Redis status job=%s",
            job_id,
            exc_info=True,
        )
    await compensate_task_run()


def deterministic_admin_rq_job_id(task_id: UUID | str, attempt: int) -> str:
    """Return the stable transport id for one durable administrator attempt."""

    return f"admin-{task_id}-attempt-{max(1, int(attempt))}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _advisory_lock_id(scope_key: str) -> int:
    import hashlib

    raw = int.from_bytes(
        hashlib.blake2b(scope_key.encode("utf-8"), digest_size=8).digest(),
        byteorder="big",
        signed=False,
    )
    return raw - (1 << 64) if raw >= (1 << 63) else raw


def _registered_spec(
    operation_type: str,
    *,
    scope_key: str,
    queue_name: str,
    legacy_function: str | None = None,
) -> AdminOperationSpec:
    spec = ADMIN_OPERATION_REGISTRY.get(operation_type)
    if spec is None:
        raise ValueError(f"Unsupported admin operation type: {operation_type}")
    if queue_name not in spec.queue_names:
        raise ValueError(
            f"Unsupported queue {queue_name!r} for admin operation {operation_type}"
        )
    if not scope_key or not any(
        scope_key == prefix or scope_key.startswith(prefix)
        for prefix in spec.scope_prefixes
    ):
        raise ValueError(
            f"Unsupported scope {scope_key!r} for admin operation {operation_type}"
        )
    if legacy_function is not None and legacy_function != spec.legacy_function:
        raise ValueError(
            f"Registered worker mismatch for admin operation {operation_type}"
        )
    return spec


def _validated_options(options: dict[str, Any] | None) -> dict[str, Any]:
    encoded = jsonable_encoder(dict(options or {}))
    if not isinstance(encoded, dict):
        raise ValueError("Administrator operation options must be an object")
    # A round trip rejects values which PostgreSQL JSONB and a later worker
    # could interpret differently. The resulting object is the worker payload.
    return json.loads(json.dumps(encoded, separators=(",", ":"), sort_keys=True))


def _admin_dispatch(task) -> dict[str, Any] | None:
    value = (task.meta or {}).get(ADMIN_DISPATCH_META_KEY)
    return dict(value) if isinstance(value, dict) else None


def _scope_for_task(task) -> str | None:
    dispatch = _admin_dispatch(task)
    if dispatch and isinstance(dispatch.get("scope_key"), str):
        return dispatch["scope_key"]
    meta = task.meta or {}
    if task.operation_type == "admin-clear" and meta.get("entity"):
        return f"library:clear:{meta['entity']}"
    if task.operation_type == "admin-gitllery-verify" and meta.get("repository_id"):
        return f"gitllery:verify:{meta['repository_id']}"
    # Rolling compatibility is PostgreSQL-only. Old active rows are still
    # considered for the fixed single-flight scopes, without consulting Redis.
    legacy = {
        "admin-rebuild": "library:rebuild:active",
        "admin-disk-import": "library:disk-import:active",
        "admin-creator-reenrich": "library:creator-reenrich:active",
        "danbooru-mapping-refresh": "library:creator-reenrich:active",
        "admin-search-reindex": "library:search-reindex:active",
        "admin-curation-backfill": "library:curation-backfill:active",
        "admin-gitllery-sync": "library:gitllery-sync:active",
        "hierarchy-delete": "library:hierarchy-delete:active",
        "asset-dedup-scan": "lock:admin:asset-dedup-scan",
    }
    return legacy.get(task.operation_type)


async def _lock_scope(db: AsyncSession, scope_key: str) -> None:
    await db.execute(select(sql_func.pg_advisory_xact_lock(_advisory_lock_id(scope_key))))


def _admin_execution_lock_id(scope_key: str) -> int:
    return _advisory_lock_id(f"admin-execution:{scope_key}")


async def _try_acquire_admin_execution_lease(
    db: AsyncSession,
    scope_key: str,
) -> bool:
    """Acquire the session lock proving no prior attempt can still execute."""

    return bool(
        (
            await db.execute(
                select(
                    sql_func.pg_try_advisory_lock(
                        _admin_execution_lock_id(scope_key)
                    )
                )
            )
        ).scalar_one()
    )


async def _release_admin_execution_lease(
    db: AsyncSession,
    scope_key: str,
) -> None:
    """Release a session lock without masking a business-path exception."""

    try:
        await db.rollback()
    except Exception:
        logger.warning(
            "Unable to roll back administrator execution lease session task=%s",
            scope_key,
            exc_info=True,
        )
    try:
        await db.execute(
            select(sql_func.pg_advisory_unlock(_admin_execution_lock_id(scope_key)))
        )
        await db.commit()
    except Exception:
        # A dead connection has already released every session advisory lock.
        logger.warning(
            "Unable to explicitly release administrator execution lease task=%s",
            scope_key,
            exc_info=True,
        )


@asynccontextmanager
async def _admin_execution_lease_on_session(
    db: AsyncSession,
    scope_key: str,
):
    acquired = await _try_acquire_admin_execution_lease(db, scope_key)
    try:
        yield acquired
    finally:
        if acquired:
            await _release_admin_execution_lease(db, scope_key)
        else:
            await db.rollback()


@asynccontextmanager
async def admin_operation_execution_lease(task_id: UUID | str):
    """Hold one PostgreSQL session lease for the complete worker execution."""

    from app.database import engine
    from app.models.task_run import TaskRun

    task_uuid = UUID(str(task_id))
    async with engine.connect() as lease_connection:
        async with AsyncSession(
            bind=lease_connection,
            expire_on_commit=False,
        ) as lease_db:
            task = await lease_db.get(TaskRun, task_uuid)
            scope_key = _scope_for_task(task) if task is not None else None
            if task is None or task.kind != "admin" or not scope_key:
                raise AdminOperationAttemptRejected(
                    "Administrator operation has no registered execution scope"
                )
            await lease_db.rollback()
            async with _admin_execution_lease_on_session(
                lease_db,
                scope_key,
            ) as acquired:
                if not acquired:
                    raise AdminOperationAttemptRejected(
                        "Administrator operation already has a live execution lease"
                    )
                # Binding the session to the explicit connection keeps the
                # session-level lock checked out across heartbeat commits.
                await lease_db.commit()
                yield lease_db


async def _active_scope_owner(db: AsyncSession, scope_key: str, *, exclude=None):
    from app.models.task_run import TaskRun

    filters = [
        TaskRun.kind == "admin",
        TaskRun.status.in_(_ACTIVE_ADMIN_STATUSES),
    ]
    if exclude is not None:
        filters.append(TaskRun.id != exclude)
    rows = list(
        (
            await db.execute(
                select(TaskRun)
                .where(*filters)
                .order_by(TaskRun.created_at.asc(), TaskRun.id.asc())
            )
        ).scalars()
    )
    return next((task for task in rows if _scope_for_task(task) == scope_key), None)


def _new_dispatch(
    *,
    task_id: UUID,
    attempt: int,
    operation_type: str,
    scope_key: str,
    queue_name: str,
    options: dict[str, Any],
    job_timeout: int,
) -> dict[str, Any]:
    now = _utcnow().isoformat()
    return {
        "version": 1,
        "operation_type": operation_type,
        "scope_key": scope_key,
        "queue_name": queue_name,
        "options": options,
        "publication_state": ADMIN_DISPATCH_PENDING,
        "publication_failures": 0,
        "next_retry_at": None,
        "next_probe_at": None,
        "last_error": None,
        "attempt": attempt,
        "rq_job_id": deterministic_admin_rq_job_id(task_id, attempt),
        "job_timeout": job_timeout,
        "prepared_at": now,
        "updated_at": now,
    }


async def prepare_admin_operation(
    db: AsyncSession,
    *,
    operation_type: str,
    scope_key: str,
    title: str,
    entity: str,
    options: dict[str, Any] | None = None,
    queue_name: str = "maintenance",
    job_timeout: int | None = None,
    task_id: UUID | None = None,
    legacy_function: str | None = None,
) -> PreparedAdminDispatch:
    """Persist one validated administrator dispatch without touching Redis."""

    from app.services.tasks import TaskService

    spec = _registered_spec(
        operation_type,
        scope_key=scope_key,
        queue_name=queue_name,
        legacy_function=legacy_function,
    )
    validated = _validated_options(options)
    timeout = int(job_timeout or spec.default_job_timeout)
    if timeout <= 0:
        raise ValueError("Administrator operation timeout must be positive")
    await _lock_scope(db, scope_key)
    owner = await _active_scope_owner(db, scope_key)
    if owner is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"{title} already running",
                "task_id": str(owner.id),
                "job_id": owner.rq_job_id,
            },
        )

    task_id = task_id or uuid4()
    attempt = 1
    dispatch = _new_dispatch(
        task_id=task_id,
        attempt=attempt,
        operation_type=operation_type,
        scope_key=scope_key,
        queue_name=queue_name,
        options=validated,
        job_timeout=timeout,
    )
    task = await TaskService(db).create_task(
        task_id=task_id,
        kind="admin",
        operation_type=operation_type,
        title=title,
        status="enqueued",
        queue_name=queue_name,
        rq_job_id=dispatch["rq_job_id"],
        progress={"phase": "enqueued", "label": f"{title} queued"},
        result={},
        meta={"entity": entity, **validated, ADMIN_DISPATCH_META_KEY: dispatch},
    )
    task.attempts = attempt
    await db.flush()
    return PreparedAdminDispatch(task, dispatch["rq_job_id"], attempt, queue_name, timeout)


async def prepare_admin_operation_retry(
    task_id: UUID | str,
) -> PreparedAdminDispatch:
    """Commit one replacement attempt before its transport handoff."""

    from app.database import engine

    task_uuid = UUID(str(task_id))
    async with engine.connect() as connection:
        async with AsyncSession(
            bind=connection,
            expire_on_commit=False,
        ) as db:
            return await _prepare_admin_operation_retry_session(db, task_uuid)


async def _prepare_admin_operation_retry_session(
    db: AsyncSession,
    task_uuid: UUID,
) -> PreparedAdminDispatch:
    from app.models.task_run import TaskRun
    from app.services.tasks import TaskService

    initial = await db.get(TaskRun, task_uuid)
    if initial is None or initial.kind != "admin":
        raise HTTPException(status_code=404, detail="Task not found")
    current_dispatch = _admin_dispatch(initial)
    if current_dispatch is None:
        raise HTTPException(
            status_code=400,
            detail="This legacy admin operation cannot be durably retried",
        )
    scope_key = str(current_dispatch.get("scope_key") or "")
    await _lock_scope(db, scope_key)
    async with _admin_execution_lease_on_session(
        db,
        scope_key,
    ) as acquired:
        if not acquired:
            raise HTTPException(
                status_code=409,
                detail="Previous administrator operation attempt is still executing",
            )
        return await _prepare_admin_operation_retry_locked(
            db,
            task_uuid=task_uuid,
            initial=initial,
            current_dispatch=current_dispatch,
            scope_key=scope_key,
            task_model=TaskRun,
            task_service=TaskService(db),
        )


async def _prepare_admin_operation_retry_locked(
    db: AsyncSession,
    *,
    task_uuid: UUID,
    initial,
    current_dispatch: dict[str, Any],
    scope_key: str,
    task_model,
    task_service,
) -> PreparedAdminDispatch:
    """Rotate a failed attempt while its scope and execution leases are held."""

    dedup_scan = None
    if initial.operation_type == "asset-dedup-scan":
        from app.models.asset_dedup import AssetDedupScan

        scan_id = (current_dispatch.get("options") or {}).get("scan_id")
        try:
            scan_uuid = UUID(str(scan_id))
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail="Asset dedup retry has no valid scan",
            ) from exc
        dedup_scan = (
            await db.execute(
                select(AssetDedupScan)
                .where(AssetDedupScan.id == scan_uuid)
                .with_for_update(of=AssetDedupScan)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if dedup_scan is None:
            raise HTTPException(
                status_code=409,
                detail="Asset dedup scan no longer exists",
            )
    task = (
        await db.execute(
            select(task_model)
            .where(task_model.id == task_uuid)
            .with_for_update(of=task_model)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in {"failed", "stale", "cancelled"}:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "invalid_task_action",
                "action": "retry",
                "status": task.status,
                "message": f"Task is {task.status}; retry is only available after failure",
            },
        )
    owner = await _active_scope_owner(db, scope_key, exclude=task.id)
    if owner is not None:
        raise HTTPException(
            status_code=409,
            detail={"message": "Operation already running", "task_id": str(owner.id)},
        )
    dispatch = _admin_dispatch(task) or {}
    next_options = _validated_options(dispatch.get("options") or {})
    if dedup_scan is not None:
        if dedup_scan.status not in {"pending", "running", "failed"}:
            raise HTTPException(
                status_code=409,
                detail=f"Asset dedup scan is {dedup_scan.status}; retry is unavailable",
            )
        scan_options = dict(dedup_scan.options or {})
        try:
            scan_generation = max(0, int(scan_options.get("_rq_generation", 0)))
        except (TypeError, ValueError):
            scan_generation = 0
        dedup_scan.status = "pending"
        dedup_scan.error = None
        next_options["scan_id"] = str(dedup_scan.id)
        next_options["_scan_generation"] = scan_generation
    attempt = max(int(task.attempts or 0), int(dispatch.get("attempt") or 0)) + 1
    next_dispatch = _new_dispatch(
        task_id=task.id,
        attempt=attempt,
        operation_type=str(task.operation_type),
        scope_key=scope_key,
        queue_name=str(dispatch.get("queue_name") or task.queue_name or "maintenance"),
        options=next_options,
        job_timeout=int(dispatch.get("job_timeout") or 14400),
    )
    checkpoints = dispatch.get("checkpoints")
    if isinstance(checkpoints, dict) and checkpoints:
        next_dispatch["checkpoints"] = _validated_options(checkpoints)
    meta = dict(task.meta or {})
    meta[ADMIN_DISPATCH_META_KEY] = next_dispatch
    task.attempts = attempt
    await task_service.update_task(
        task,
        status="enqueued",
        progress={"phase": "enqueued", "label": f"{task.title or 'Operation'} queued"},
        result={},
        error="",
        meta=meta,
        rq_job_id=next_dispatch["rq_job_id"],
    )
    await db.commit()
    return PreparedAdminDispatch(
        task,
        next_dispatch["rq_job_id"],
        attempt,
        next_dispatch["queue_name"],
        next_dispatch["job_timeout"],
    )


async def prepare_admin_operation_handoff(
    db: AsyncSession,
    task_id: UUID | str,
    attempt: int,
    *,
    options: dict[str, Any],
    delay_seconds: float,
    progress: dict[str, Any] | None = None,
) -> PreparedAdminDispatch | None:
    """Atomically persist a bounded worker's next delivery before publishing."""

    from app.models.task_run import TaskRun
    from app.services.tasks import TaskService

    task_uuid = UUID(str(task_id))
    task = (
        await db.execute(
            select(TaskRun)
            .where(TaskRun.id == task_uuid)
            .with_for_update(of=TaskRun)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    dispatch = _admin_dispatch(task) if task is not None else None
    if (
        task is None
        or task.kind != "admin"
        or dispatch is None
        or int(dispatch.get("attempt") or 0) != int(attempt)
        or task.status not in _ACTIVE_ADMIN_STATUSES
    ):
        return None

    next_attempt = int(attempt) + 1
    next_dispatch = _new_dispatch(
        task_id=task.id,
        attempt=next_attempt,
        operation_type=str(task.operation_type),
        scope_key=str(dispatch["scope_key"]),
        queue_name=str(dispatch["queue_name"]),
        options=_validated_options(options),
        job_timeout=int(dispatch["job_timeout"]),
    )
    next_dispatch["next_retry_at"] = (
        _utcnow()
        + timedelta(
            seconds=max(
                1.0,
                min(float(delay_seconds), ADMIN_DISPATCH_RETRY_MAX_SECONDS),
            )
        )
    ).isoformat()
    await TaskService(db).update_task(
        task,
        status="enqueued",
        progress=progress or {"phase": "enqueued", "label": "Next slice queued"},
        error="",
    )
    meta = dict(task.meta or {})
    meta[ADMIN_DISPATCH_META_KEY] = next_dispatch
    task.meta = meta
    task.attempts = next_attempt
    task.rq_job_id = next_dispatch["rq_job_id"]
    await db.flush()
    return PreparedAdminDispatch(
        task,
        next_dispatch["rq_job_id"],
        next_attempt,
        next_dispatch["queue_name"],
        next_dispatch["job_timeout"],
    )


async def prepare_admin_operation_recovery(
    task_id: UUID | str,
    attempt: int,
    *,
    now: datetime | None = None,
) -> PreparedAdminDispatch | None:
    """Fence a stale running delivery before publishing its replacement."""

    from app.database import engine

    task_uuid = UUID(str(task_id))
    checked_at = now or _utcnow()
    async with engine.connect() as connection:
        async with AsyncSession(
            bind=connection,
            expire_on_commit=False,
        ) as db:
            return await _prepare_admin_operation_recovery_session(
                db,
                task_uuid=task_uuid,
                attempt=int(attempt),
                checked_at=checked_at,
            )


async def _prepare_admin_operation_recovery_session(
    db: AsyncSession,
    *,
    task_uuid: UUID,
    attempt: int,
    checked_at: datetime,
) -> PreparedAdminDispatch | None:
    from app.models.task_run import TaskRun
    from app.services.tasks import TaskService

    initial = await db.get(TaskRun, task_uuid)
    initial_dispatch = _admin_dispatch(initial) if initial is not None else None
    if initial_dispatch is None:
        return None
    scope_key = str(initial_dispatch.get("scope_key") or "")
    await _lock_scope(db, scope_key)
    async with _admin_execution_lease_on_session(
        db,
        scope_key,
    ) as acquired:
        if not acquired:
            return None
        return await _prepare_admin_operation_recovery_locked(
            db,
            task_uuid=task_uuid,
            attempt=attempt,
            checked_at=checked_at,
            scope_key=scope_key,
            task_model=TaskRun,
            task_service=TaskService(db),
        )


async def _prepare_admin_operation_recovery_locked(
    db: AsyncSession,
    *,
    task_uuid: UUID,
    attempt: int,
    checked_at: datetime,
    scope_key: str,
    task_model,
    task_service,
) -> PreparedAdminDispatch | None:
    task = (
        await db.execute(
            select(task_model)
            .where(task_model.id == task_uuid)
            .with_for_update(of=task_model)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    dispatch = _admin_dispatch(task) if task is not None else None
    if (
        task is None
        or task.kind != "admin"
        or task.status != "running"
        or dispatch is None
        or int(dispatch.get("attempt") or 0) != attempt
    ):
        await db.rollback()
        return None
    heartbeat_at = task.last_heartbeat_at
    if (
        heartbeat_at is not None
        and heartbeat_at
        > checked_at - timedelta(seconds=ADMIN_ATTEMPT_HEARTBEAT_STALE_SECONDS)
    ):
        await db.rollback()
        return None

    next_attempt = attempt + 1
    next_dispatch = _new_dispatch(
        task_id=task.id,
        attempt=next_attempt,
        operation_type=str(task.operation_type),
        scope_key=scope_key,
        queue_name=str(dispatch.get("queue_name") or task.queue_name or "maintenance"),
        options=_validated_options(dispatch.get("options") or {}),
        job_timeout=int(dispatch.get("job_timeout") or 14400),
    )
    checkpoints = dispatch.get("checkpoints")
    if isinstance(checkpoints, dict) and checkpoints:
        next_dispatch["checkpoints"] = _validated_options(checkpoints)
    next_dispatch["recovered_from_attempt"] = attempt
    meta = dict(task.meta or {})
    meta[ADMIN_DISPATCH_META_KEY] = next_dispatch
    task.attempts = next_attempt
    task.last_heartbeat_at = None
    await task_service.update_task(
        task,
        status="enqueued",
        progress={
            "phase": "enqueued",
            "label": f"{task.title or 'Operation'} recovered",
        },
        error="",
        meta=meta,
        rq_job_id=next_dispatch["rq_job_id"],
    )
    await db.commit()
    return PreparedAdminDispatch(
        task,
        next_dispatch["rq_job_id"],
        next_attempt,
        next_dispatch["queue_name"],
        next_dispatch["job_timeout"],
    )


def _fetch_admin_rq(rq_job_id: str, *, redis_client=None):
    from rq.exceptions import NoSuchJobError
    from rq.job import Job

    try:
        return Job.fetch(
            rq_job_id,
            connection=redis_client if redis_client is not None else get_redis(),
        )
    except NoSuchJobError:
        return None


def _rq_status(job) -> str:
    status = job.get_status(refresh=True)
    return str(getattr(status, "value", status)).lower()


def _enqueue_admin_rq(
    task_id: UUID | str,
    attempt: int,
    *,
    operation_type: str,
    rq_job_id: str,
    queue_name: str,
    job_timeout: int,
    redis_client=None,
):
    from rq import Queue

    connection = redis_client if redis_client is not None else get_redis()
    metadata = {"registered_admin_operation": operation_type}
    internal_profile = _ADMIN_INTERNAL_RESOURCE_PROFILES.get(operation_type)
    if internal_profile is not None:
        metadata["registered_admin_internal_profile"] = internal_profile
    return checked_enqueue(
        Queue(name=queue_name, connection=connection),
        ADMIN_RQ_FUNCTION,
        str(task_id),
        int(attempt),
        job_id=rq_job_id,
        job_timeout=int(job_timeout),
        result_ttl=7 * 24 * 60 * 60,
        failure_ttl=7 * 24 * 60 * 60,
        description=f"admin task={task_id} attempt={attempt}",
        meta=metadata,
    )


def _publication_retry_delay(failures: int) -> int:
    exponent = max(0, int(failures) - 1)
    if exponent >= 5:
        return ADMIN_DISPATCH_RETRY_MAX_SECONDS
    return min(
        ADMIN_DISPATCH_RETRY_MAX_SECONDS,
        ADMIN_DISPATCH_RETRY_MIN_SECONDS * (2 ** exponent),
    )


async def publish_admin_operation(
    task_id: UUID | str,
    attempt: int,
    *,
    redis_client=None,
) -> str:
    """Publish one committed attempt idempotently and persist its acknowledgement."""

    import asyncio

    from app.database import async_session
    from app.models.task_run import TaskRun
    from app.services.tasks import TaskService

    task_uuid = UUID(str(task_id))
    async with async_session() as db:
        task = (
            await db.execute(
                select(TaskRun)
                .where(TaskRun.id == task_uuid)
                .with_for_update(of=TaskRun)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        dispatch = _admin_dispatch(task) if task is not None else None
        if (
            task is None
            or task.kind != "admin"
            or dispatch is None
            or int(dispatch.get("attempt") or 0) != int(attempt)
            or task.rq_job_id != dispatch.get("rq_job_id")
            or task.status not in _ACTIVE_ADMIN_STATUSES
        ):
            await db.rollback()
            return "skipped"
        rq_job_id = str(dispatch["rq_job_id"])
        try:
            existing = await asyncio.to_thread(
                _fetch_admin_rq,
                rq_job_id,
                redis_client=redis_client,
            )
            if existing is not None:
                status = await asyncio.to_thread(_rq_status, existing)
                if status not in {"queued", "started", "deferred", "scheduled"}:
                    message = f"RQ attempt ended as {status} without a current TaskRun callback"
                    dispatch["publication_state"] = ADMIN_DISPATCH_FAILED
                    dispatch["updated_at"] = _utcnow().isoformat()
                    meta = dict(task.meta or {})
                    meta[ADMIN_DISPATCH_META_KEY] = dispatch
                    await TaskService(db).update_task(
                        task,
                        status="failed",
                        result=task.result_data,
                        error=message,
                        meta=meta,
                        reason_code="rq_terminal_without_callback",
                    )
                    await db.commit()
                    return "failed"
                outcome = "existing"
            elif (
                dispatch.get("publication_state") == ADMIN_DISPATCH_PUBLISHED
                and task.status in {"running", "paused"}
            ):
                now_value = _utcnow()
                is_paused = task.status == "paused"
                heartbeat_at = task.last_heartbeat_at
                heartbeat_fresh = bool(
                    heartbeat_at is not None
                    and heartbeat_at
                    > now_value
                    - timedelta(seconds=ADMIN_ATTEMPT_HEARTBEAT_STALE_SECONDS)
                )
                dispatch.update(
                    {
                        "next_probe_at": (
                            now_value
                            + timedelta(seconds=ADMIN_DISPATCH_RECOVERY_INTERVAL_SECONDS)
                        ).isoformat(),
                        "last_error": "RQ record missing while durable attempt is active",
                        "updated_at": now_value.isoformat(),
                    }
                )
                meta = dict(task.meta or {})
                meta[ADMIN_DISPATCH_META_KEY] = dispatch
                task.meta = meta
                await db.commit()
                return "active" if is_paused or heartbeat_fresh else "missing"
            else:
                await asyncio.to_thread(
                    _enqueue_admin_rq,
                    task.id,
                    attempt,
                    operation_type=str(dispatch["operation_type"]),
                    rq_job_id=rq_job_id,
                    queue_name=str(dispatch["queue_name"]),
                    job_timeout=int(dispatch["job_timeout"]),
                    redis_client=redis_client,
                )
                outcome = "published"
        except Exception as exc:
            failures = int(dispatch.get("publication_failures") or 0) + 1
            dispatch.update(
                {
                    "publication_state": ADMIN_DISPATCH_PENDING,
                    "publication_failures": failures,
                    "last_error": str(exc)[:1000],
                    "last_error_type": type(exc).__name__,
                    "next_retry_at": (
                        _utcnow() + timedelta(seconds=_publication_retry_delay(failures))
                    ).isoformat(),
                    "updated_at": _utcnow().isoformat(),
                }
            )
            meta = dict(task.meta or {})
            meta[ADMIN_DISPATCH_META_KEY] = dispatch
            task.meta = meta
            await db.commit()
            logger.warning(
                "Admin dispatch deferred task=%s attempt=%s error=%s",
                task.id,
                attempt,
                type(exc).__name__,
            )
            return "deferred"

        now_value = _utcnow()
        now = now_value.isoformat()
        dispatch.update(
            {
                "publication_state": ADMIN_DISPATCH_PUBLISHED,
                "published_at": now,
                "next_retry_at": None,
                "next_probe_at": (
                    now_value
                    + timedelta(seconds=ADMIN_DISPATCH_RECOVERY_INTERVAL_SECONDS)
                ).isoformat(),
                "last_error": None,
                "updated_at": now,
            }
        )
        meta = dict(task.meta or {})
        meta[ADMIN_DISPATCH_META_KEY] = dispatch
        task.meta = meta
        await db.commit()
        return outcome


async def start_admin_operation(
    *,
    operation_type: str,
    scope_key: str,
    title: str,
    entity: str,
    options: dict[str, Any] | None = None,
    job_timeout: int | None = None,
    queue_name: str = "maintenance",
    legacy_function: str | None = None,
    redis_client=None,
) -> dict[str, Any]:
    """Commit a TaskRun first, then best-effort publish its current attempt."""

    from app.database import async_session

    async with async_session() as db:
        prepared = await prepare_admin_operation(
            db,
            operation_type=operation_type,
            scope_key=scope_key,
            title=title,
            entity=entity,
            options=options,
            queue_name=queue_name,
            job_timeout=job_timeout,
            legacy_function=legacy_function,
        )
        await db.commit()
        task_id = prepared.task.id
    publication = await publish_admin_operation(
        task_id,
        prepared.attempt,
        redis_client=redis_client,
    )
    return {
        "task_id": str(task_id),
        "job_id": prepared.rq_job_id,
        "status": "enqueued",
        "operation_type": operation_type,
        "publication_state": (
            ADMIN_DISPATCH_PUBLISHED
            if publication in {"published", "existing"}
            else ADMIN_DISPATCH_PENDING
        ),
    }


async def retry_admin_operation(
    task_id: UUID | str,
    *,
    redis_client=None,
) -> dict[str, Any]:
    prepared = await prepare_admin_operation_retry(task_id)
    publication = await publish_admin_operation(
        prepared.task.id,
        prepared.attempt,
        redis_client=redis_client,
    )
    return {
        "task_id": str(prepared.task.id),
        "job_id": prepared.rq_job_id,
        "status": "enqueued",
        "operation_type": prepared.task.operation_type,
        "publication_state": (
            ADMIN_DISPATCH_PUBLISHED
            if publication in {"published", "existing"}
            else ADMIN_DISPATCH_PENDING
        ),
    }


async def latest_successful_admin_operation(
    db: AsyncSession,
    *,
    operation_type: str,
    scope_key: str,
) -> dict[str, Any]:
    """Return the latest completed TaskRun result for one registered scope."""

    from app.models.task_run import TaskRun

    _registered_spec(
        operation_type,
        scope_key=scope_key,
        queue_name="maintenance",
    )
    scope_text = TaskRun.meta[ADMIN_DISPATCH_META_KEY]["scope_key"].astext
    snapshot_task = (
        await db.execute(
            select(TaskRun)
            .where(
                TaskRun.kind == "admin",
                TaskRun.operation_type == operation_type,
                TaskRun.status == "complete",
                scope_text == scope_key,
                TaskRun.finished_at.is_not(None),
            )
            .order_by(TaskRun.finished_at.desc(), TaskRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    current_task = (
        await db.execute(
            select(TaskRun)
            .where(
                TaskRun.kind == "admin",
                TaskRun.operation_type == operation_type,
                TaskRun.status.in_(_ACTIVE_ADMIN_STATUSES),
                scope_text == scope_key,
            )
            .order_by(TaskRun.created_at.desc(), TaskRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    snapshot = None
    if snapshot_task is not None:
        snapshot = {
            "task_id": str(snapshot_task.id),
            "job_id": snapshot_task.rq_job_id,
            "status": "complete",
            "operation_type": str(snapshot_task.operation_type),
            "progress": snapshot_task.progress_data,
            "result": dict(snapshot_task.result_data or {}),
            "completed_at": snapshot_task.finished_at,
        }
    current = None
    if current_task is not None:
        current = {
            "task_id": str(current_task.id),
            "job_id": current_task.rq_job_id,
            "status": str(current_task.status),
            "operation_type": str(current_task.operation_type),
            "progress": current_task.progress_data,
        }
    return {
        "snapshot": snapshot,
        "current": current,
    }


def _parse_dispatch_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


async def recover_admin_operation_dispatches(
    *,
    redis_client=None,
    now: datetime | None = None,
    grace_seconds: int = ADMIN_DISPATCH_GRACE_SECONDS,
    limit: int = ADMIN_DISPATCH_RECOVERY_LIMIT,
    include_published: bool = False,
) -> dict[str, int]:
    """Repair at most 25 due TaskRun publication intents in one pass."""

    from app.database import async_session
    from app.models.task_run import TaskRun

    current = now or _utcnow()
    bounded_limit = max(1, min(int(limit), ADMIN_DISPATCH_RECOVERY_LIMIT))
    prepared_text = TaskRun.meta[ADMIN_DISPATCH_META_KEY]["prepared_at"].astext
    prepared_at = cast(prepared_text, DateTime(timezone=True))
    retry_text = TaskRun.meta[ADMIN_DISPATCH_META_KEY]["next_retry_at"].astext
    retry_at = cast(retry_text, DateTime(timezone=True))
    grace_cutoff = current - timedelta(seconds=max(0, grace_seconds))
    common_filters = (
        TaskRun.kind == "admin",
        TaskRun.status.in_(_ACTIVE_ADMIN_STATUSES),
        prepared_at <= grace_cutoff,
    )

    async with async_session() as db:
        pending = list(
            (
                await db.execute(
                    select(TaskRun)
                    .where(
                        *common_filters,
                        TaskRun.meta[ADMIN_DISPATCH_META_KEY]["publication_state"]
                        .astext
                        == ADMIN_DISPATCH_PENDING,
                        or_(retry_text.is_(None), retry_at <= current),
                    )
                    .order_by(
                        retry_at.asc().nullsfirst(),
                        prepared_at.asc(),
                        TaskRun.id.asc(),
                    )
                    .limit(bounded_limit)
                )
            ).scalars()
        )
        due = [
            (task.id, int((_admin_dispatch(task) or {}).get("attempt") or 0))
            for task in pending
        ]

        remaining = bounded_limit - len(due)
        if include_published and remaining > 0:
            probe_text = TaskRun.meta[ADMIN_DISPATCH_META_KEY]["next_probe_at"].astext
            probe_at = cast(probe_text, DateTime(timezone=True))
            published = list(
                (
                    await db.execute(
                        select(TaskRun)
                        .where(
                            *common_filters,
                            TaskRun.meta[ADMIN_DISPATCH_META_KEY][
                                "publication_state"
                            ].astext
                            == ADMIN_DISPATCH_PUBLISHED,
                            or_(probe_text.is_(None), probe_at <= current),
                        )
                        .order_by(
                            probe_at.asc().nullsfirst(),
                            prepared_at.asc(),
                            TaskRun.id.asc(),
                        )
                        .limit(remaining)
                    )
                ).scalars()
            )
            due.extend(
                (
                    task.id,
                    int((_admin_dispatch(task) or {}).get("attempt") or 0),
                )
                for task in published
            )

    report = {"scanned": len(due), "published": 0, "deferred": 0, "failed": 0}
    for candidate_id, attempt in due:
        outcome = await publish_admin_operation(
            candidate_id,
            attempt,
            redis_client=redis_client,
        )
        if outcome == "missing":
            recovered = await prepare_admin_operation_recovery(
                candidate_id,
                attempt,
                now=current,
            )
            if recovered is not None:
                outcome = await publish_admin_operation(
                    candidate_id,
                    recovered.attempt,
                    redis_client=redis_client,
                )
            else:
                outcome = "active"
        if outcome in {"published", "existing"}:
            report["published"] += 1
        elif outcome == "deferred":
            report["deferred"] += 1
        elif outcome == "failed":
            report["failed"] += 1
    return report


async def update_admin_task(
    task_id: UUID | str,
    attempt: int | str | None,
    *,
    allowed_current_statuses: Iterable[str] | None = None,
    **changes: Any,
) -> bool:
    """Apply a worker callback only for its exact durable current attempt."""

    from app.database import async_session
    from app.models.task_run import TaskRun
    from app.services.tasks import TaskService

    try:
        task_uuid = UUID(str(task_id))
        captured_attempt = int(attempt) if attempt is not None else None
    except (TypeError, ValueError):
        return False
    async with async_session() as db:
        task = (
            await db.execute(
                select(TaskRun)
                .where(TaskRun.id == task_uuid)
                .with_for_update(of=TaskRun)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        dispatch = _admin_dispatch(task) if task is not None else None
        if dispatch is not None:
            if captured_attempt is None or int(dispatch.get("attempt") or 0) != captured_attempt:
                await db.rollback()
                return False
        elif captured_attempt is not None:
            await db.rollback()
            return False
        if task is None or task.kind != "admin":
            await db.rollback()
            return False
        if (
            allowed_current_statuses is not None
            and task.status
            not in frozenset(str(status) for status in allowed_current_statuses)
        ):
            await db.rollback()
            return False
        await TaskService(db).update_task(task, **changes)
        await db.commit()
        return True


async def heartbeat_admin_operation(
    task_id: UUID | str,
    attempt: int | str,
    *,
    db: AsyncSession | None = None,
) -> bool:
    """Renew the PostgreSQL execution lease for exactly one running attempt."""

    from app.database import async_session
    from app.models.task_run import TaskRun

    try:
        task_uuid = UUID(str(task_id))
        captured_attempt = int(attempt)
    except (TypeError, ValueError):
        return False
    async def renew(session: AsyncSession) -> bool:
        task = (
            await session.execute(
                select(TaskRun)
                .where(TaskRun.id == task_uuid)
                .with_for_update(of=TaskRun)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        dispatch = _admin_dispatch(task) if task is not None else None
        if (
            task is None
            or task.kind != "admin"
            or task.status != "running"
            or dispatch is None
            or int(dispatch.get("attempt") or 0) != captured_attempt
        ):
            await session.rollback()
            return False
        task.last_heartbeat_at = _utcnow()
        await session.commit()
        return True

    if db is not None:
        return await renew(db)
    async with async_session() as heartbeat_db:
        return await renew(heartbeat_db)


async def update_current_admin_operation_progress(
    task_id: UUID | str,
    progress: dict[str, Any],
) -> None:
    """Durably update progress for the registered attempt in this context."""

    delivery = current_admin_operation_attempt()
    if delivery is None or delivery[0] != UUID(str(task_id)):
        raise AdminOperationAttemptRejected(
            "Administrator operation progress has no current attempt"
        )
    if not await update_admin_task(
        delivery[0],
        delivery[1],
        allowed_current_statuses=_ACTIVE_ADMIN_STATUSES,
        status="running",
        progress=_validated_options(progress),
    ):
        raise AdminOperationAttemptRejected(
            "Administrator operation attempt is no longer current"
        )


async def fence_current_admin_operation_transaction(
    db: AsyncSession,
    *,
    task_id: UUID | str | None = None,
    allowed_statuses: Iterable[str] = _ACTIVE_ADMIN_STATUSES,
):
    """Lock and validate the current TaskRun as the final row in a business tx.

    Registered handlers call this immediately before each domain commit.  The
    TaskRun lock is deliberately acquired after domain rows to preserve the
    global lock order, and a superseded delivery raises a typed exception so a
    broad best-effort handler cannot turn fencing into an item-level warning.
    Legacy handlers without a registered context remain unaffected.
    """

    from app.models.task_run import TaskRun

    delivery = current_admin_operation_attempt()
    if delivery is None:
        return None
    if task_id is not None and delivery[0] != UUID(str(task_id)):
        raise AdminOperationAttemptRejected(
            "Administrator operation transaction belongs to another task"
        )
    task = (
        await db.execute(
            select(TaskRun)
            .where(TaskRun.id == delivery[0])
            .with_for_update(of=TaskRun)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    dispatch = _admin_dispatch(task) if task is not None else None
    allowed = frozenset(str(status) for status in allowed_statuses)
    if (
        task is None
        or task.kind != "admin"
        or dispatch is None
        or int(dispatch.get("attempt") or 0) != delivery[1]
        or task.status not in allowed
    ):
        raise AdminOperationAttemptRejected(
            "Administrator operation attempt is no longer current"
        )
    return task


async def get_current_admin_operation_checkpoint(
    db: AsyncSession,
    name: str,
) -> dict[str, Any] | None:
    """Read one checkpoint only from the exact registered TaskRun attempt."""

    from app.models.task_run import TaskRun

    delivery = current_admin_operation_attempt()
    if delivery is None:
        return None
    task = await db.get(TaskRun, delivery[0], populate_existing=True)
    dispatch = _admin_dispatch(task) if task is not None else None
    if dispatch is None or int(dispatch.get("attempt") or 0) != delivery[1]:
        raise AdminOperationAttemptRejected(
            "Administrator operation attempt is no longer current"
        )
    checkpoint = (dispatch.get("checkpoints") or {}).get(name)
    return _validated_options(checkpoint) if isinstance(checkpoint, dict) else None


async def set_current_admin_operation_checkpoint(
    db: AsyncSession,
    name: str,
    checkpoint: dict[str, Any] | None,
    *,
    progress: dict[str, Any] | None = None,
) -> None:
    """Persist or clear a checkpoint below the exact current TaskRun attempt."""

    from app.models.task_run import TaskRun
    from app.services.tasks import TaskService

    delivery = current_admin_operation_attempt()
    if delivery is None:
        raise AdminOperationAttemptRejected(
            "Administrator operation checkpoint has no current attempt"
        )
    task = await fence_current_admin_operation_transaction(db)
    dispatch = _admin_dispatch(task)
    checkpoints = dict(dispatch.get("checkpoints") or {})
    if checkpoint is None:
        checkpoints.pop(name, None)
    else:
        checkpoints[name] = _validated_options(checkpoint)
    dispatch["checkpoints"] = checkpoints
    dispatch["updated_at"] = _utcnow().isoformat()
    meta = dict(task.meta or {})
    meta[ADMIN_DISPATCH_META_KEY] = dispatch
    await TaskService(db).update_task(
        task,
        progress=_validated_options(progress) if progress is not None else None,
        meta=meta,
    )


async def claim_admin_operation(
    task_id: UUID | str,
    attempt: int | str,
) -> tuple[str, dict[str, Any]]:
    """Load validated worker arguments while atomically claiming this attempt."""

    from app.database import async_session
    from app.models.task_run import TaskRun
    from app.services.tasks import TaskService

    task_uuid = UUID(str(task_id))
    captured_attempt = int(attempt)
    async with async_session() as db:
        task = (
            await db.execute(
                select(TaskRun)
                .where(TaskRun.id == task_uuid)
                .with_for_update(of=TaskRun)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        dispatch = _admin_dispatch(task) if task is not None else None
        if (
            task is None
            or task.kind != "admin"
            or dispatch is None
            or int(dispatch.get("attempt") or 0) != captured_attempt
        ):
            await db.rollback()
            raise RuntimeError("Administrator operation attempt is no longer current")
        if task.status not in {"enqueued", "recovering"}:
            current_status = str(task.status)
            await db.rollback()
            raise RuntimeError(
                f"Administrator operation attempt cannot run from {current_status}"
            )
        operation_type = str(task.operation_type or dispatch.get("operation_type") or "")
        options = _validated_options(dispatch.get("options") or {})
        spec = _registered_spec(
            operation_type,
            scope_key=str(dispatch.get("scope_key") or ""),
            queue_name=str(dispatch.get("queue_name") or ""),
        )
        if task.queue_name not in spec.queue_names:
            await db.rollback()
            raise RuntimeError("Administrator operation queue is no longer registered")
        await TaskService(db).update_task(
            task,
            status="running",
            progress={"phase": "running", "label": f"{task.title or 'Operation'} running"},
        )
        task.last_heartbeat_at = _utcnow()
        await db.commit()
        return operation_type, options


async def enqueue_admin_operation(
    *,
    lock_key: str,
    operation_type: str,
    title: str,
    entity: str,
    func: str,
    options: dict[str, Any] | None = None,
    job_timeout: int = 14400,
    queue_name: str = "operations",
) -> dict[str, Any]:
    """Backward-compatible caller surface backed solely by PostgreSQL authority."""

    return await start_admin_operation(
        operation_type=operation_type,
        scope_key=lock_key,
        title=title,
        entity=entity,
        options=options,
        job_timeout=job_timeout,
        queue_name=queue_name,
        legacy_function=func,
    )
