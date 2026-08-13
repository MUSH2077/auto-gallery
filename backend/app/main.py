import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api import api_router
from app.api.media import router as media_router
from app.api_docs import create_api_docs_router
from app.config import settings
from app.database import async_session, engine
from app.openapi_contract import build_openapi_schema, stable_operation_id
from app.services.danbooru import DanbooruUnavailableError
from app.services.queue_admission import QueueAdmissionError

# Configure stdlib logging from settings.
# NOTE: do NOT use logging.basicConfig() here — it is a no-op once the root
# logger already has a handler, which silently left the root level at WARNING
# (dropping every INFO log) and added no stdout handler (so `docker compose
# logs backend` showed nothing from the app). We configure the root logger
# explicitly instead.
import logging.handlers, os as _os, sys as _sys

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
_root_logger = logging.getLogger()
_root_logger.setLevel(_log_level)

# 1. stdout — this is what `docker compose logs backend` captures. Primary
#    place to read/copy logs.
_stream_handler = logging.StreamHandler(_sys.stdout)
_stream_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_root_logger.addHandler(_stream_handler)

# 2. rotating file for persistence across restarts (under APP_CONFIG_ROOT/logs).
try:
    _log_dir = _os.path.join(settings.app_config_root, "logs")
    _os.makedirs(_log_dir, exist_ok=True)
    _file_handler = logging.handlers.RotatingFileHandler(
        _os.path.join(_log_dir, "backend.log"), maxBytes=10 * 1024 * 1024, backupCount=5
    )
    _file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    _root_logger.addHandler(_file_handler)
except OSError:
    pass

# 3. Route uvicorn's own loggers through the root handlers so access/error
#    lines land in the same stream + file at the same level.
for _uv_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _uv_logger = logging.getLogger(_uv_name)
    _uv_logger.handlers = []
    _uv_logger.propagate = True

# Install in-memory ring buffer for log API
from app.services.log_buffer import install as install_log_buffer
install_log_buffer()

# Bridge structlog -> stdlib so all loggers output consistently
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)
logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Critical service secrets are validated at config load time; the admin
    # bootstrap password may use the documented first-login default and is
    # always forced through password rotation.
    logger.info("backend starting", log_level=settings.log_level)
    from app.auth import ensure_admin_user
    from app.services.settings import ensure_gallerydl_config
    try:
        ensure_gallerydl_config()
    except Exception as e:
        logger.warning("ensure_gallerydl_config failed", error=str(e))
    try:
        await ensure_admin_user()
    except Exception as e:
        logger.warning("ensure_admin_user failed (may be first run before migration)", error=str(e))

    # Operation state is intentionally retained across API restarts. Workers
    # own terminal transitions and long-running rebuilds resume from checkpoints.

    # ── Start WebSocket Redis listener ──
    from app.services.ws_manager import manager as ws_manager
    ws_task = asyncio.create_task(ws_manager.start_redis_listener())
    logger.info("WebSocket Redis listener started")

    # ── Start stale detection background task ──
    async def stale_detection_loop():
        while True:
            # Redis heartbeat keys live for 90 seconds.  One bounded pass per
            # TTL window is enough; detect_stale_tasks returns after two cheap
            # active-row queries when no worker is running.
            await asyncio.sleep(90)
            try:
                async with async_session() as db:
                    from app.services.task_engine import TaskEngine
                    engine = TaskEngine(db)
                    count = await engine.detect_stale_tasks()
                    if count:
                        logger.info("Stale detection: marked %d tasks as stale", count)
            except Exception:
                logger.warning("Stale detection cycle failed", exc_info=True)

    stale_task = asyncio.create_task(stale_detection_loop())
    logger.info("Stale detection background task started")

    async def operation_maintenance_loop():
        from app.services.operation_attention import (
            compact_terminal_tasks,
            reconcile_task_truth,
        )

        reconcile_interval = max(
            30.0,
            float(settings.task_reconciliation_interval_seconds),
        )
        compact_interval = max(
            reconcile_interval,
            float(settings.task_compaction_interval_seconds),
        )
        next_compaction = time.monotonic() + compact_interval
        while True:
            await asyncio.sleep(reconcile_interval)
            try:
                async with async_session() as db:
                    result = await reconcile_task_truth(db, dry_run=False, limit=500)
                if result.get("corrected"):
                    logger.info("Task truth reconciliation", **result)
            except Exception:
                logger.warning("Task truth reconciliation failed", exc_info=True)

            if (
                not settings.task_compaction_enabled
                or time.monotonic() < next_compaction
            ):
                continue
            next_compaction = time.monotonic() + compact_interval
            try:
                async with async_session() as db:
                    result = await compact_terminal_tasks(
                        db,
                        dry_run=False,
                        limit=settings.task_compaction_batch_size,
                    )
                if result.get("deleted_tasks"):
                    logger.info("Terminal task compaction", **result)
            except Exception:
                logger.warning("Terminal task compaction failed", exc_info=True)

    operation_maintenance_task = asyncio.create_task(operation_maintenance_loop())
    logger.info(
        "Task reconciliation/compaction started (%.0fs/%.0fs)",
        max(30.0, float(settings.task_reconciliation_interval_seconds)),
        max(
            max(30.0, float(settings.task_reconciliation_interval_seconds)),
            float(settings.task_compaction_interval_seconds),
        ),
    )

    async def import_recovery_loop():
        while True:
            await asyncio.sleep(120)
            try:
                async with async_session() as db:
                    from app.services.import_recovery import recover_import_pipeline
                    from app.services.redis_client import get_redis
                    result = await recover_import_pipeline(db, redis_client=get_redis())
                    if result.get("imports_enqueued") or result.get("imports_replayed"):
                        logger.info("Import recovery repaired pipeline gap", **result)
            except Exception:
                logger.warning("Import recovery cycle failed", exc_info=True)

    import_recovery_task = asyncio.create_task(import_recovery_loop())
    logger.info("Import recovery background task started")

    async def download_dispatch_recovery_loop():
        # TaskRun.meta is the lightweight publication outbox. A short bounded
        # scan repairs the commit->Redis crash window without adding another
        # table or increasing normal request latency.
        recovery_interval = max(
            10.0,
            float(settings.download_dispatch_recovery_interval_seconds),
        )
        while True:
            await asyncio.sleep(recovery_interval)
            try:
                async with async_session() as db:
                    from app.services.download_dispatch import recover_download_dispatch_outbox
                    from app.services.redis_client import get_redis

                    result = await recover_download_dispatch_outbox(
                        db,
                        redis_client=get_redis(),
                    )
                    if (
                        result.get("replayed")
                        or result.get("terminal")
                        or result.get("deferred")
                        or result.get("invalid")
                    ):
                        logger.info("Download dispatch outbox recovery cycle", **result)
            except Exception:
                logger.warning("Download dispatch outbox recovery failed", exc_info=True)

    download_dispatch_recovery_task = asyncio.create_task(download_dispatch_recovery_loop())
    logger.info(
        "Download dispatch outbox recovery started (every %.0fs, grace %.0fs, batch %d)",
        max(10.0, float(settings.download_dispatch_recovery_interval_seconds)),
        settings.download_dispatch_recovery_grace_seconds,
        settings.download_dispatch_recovery_batch_size,
    )

    async def outbox_coordinator_loop():
        # HTTP workers only publish bounded wake-ups. libvips/ffmpeg/Gitllery
        # and deep dedup always execute in an RQ workhorse with a resource
        # profile; an idle system performs one compact SQL snapshot and zero
        # Redis writes per cycle.
        while True:
            await asyncio.sleep(15)
            try:
                from app.services.outbox_coordinator import (
                    outbox_counts,
                    wake_pending_outboxes,
                )

                async with async_session() as db:
                    counts = await outbox_counts(db)
                if any(counts.values()):
                    result = wake_pending_outboxes(counts)
                    logger.info("Pipeline outbox coordinator", counts=counts, **result)
            except Exception:
                logger.warning("Pipeline outbox coordinator failed", exc_info=True)

    outbox_coordinator_task = asyncio.create_task(outbox_coordinator_loop())
    logger.info("Pipeline outbox coordinator started (15s snapshots)")

    # ── Memory monitor — logs RSS so an OOM leaves a visible climb in the
    #    logs + the last thing that was happening. WARNs past a threshold. ──
    async def memory_monitor_loop():
        warned = False
        while True:
            await asyncio.sleep(settings.memory_log_interval_seconds)
            try:
                rss = _process_rss_mb()
                if rss is None:
                    continue
                if rss >= settings.memory_warn_mb:
                    logger.warning(
                        "High backend memory: RSS=%.0fMB (warn threshold=%dMB). "
                        "GET /api/v1/admin/memory for a top-object census.",
                        rss, settings.memory_warn_mb)
                    warned = True
                else:
                    if warned:
                        logger.info("Backend memory back to normal: RSS=%.0fMB", rss)
                        warned = False
                    logger.info("Backend memory: RSS=%.0fMB", rss)
            except Exception:
                logger.warning("Memory monitor cycle failed", exc_info=True)

    memory_task = asyncio.create_task(memory_monitor_loop())
    logger.info("Memory monitor background task started (warn>=%dMB, every %ds)",
                settings.memory_warn_mb, settings.memory_log_interval_seconds)

    # Host-wide memory/swap/PSI guard.  The latest sample is shared through
    # Redis so every RQ process makes the same admission decision without
    # requiring Docker socket access.
    from app.services.resource_pressure import resource_pressure_monitor_loop
    resource_pressure_task = asyncio.create_task(resource_pressure_monitor_loop())
    logger.info(
        "Resource pressure monitor started (every %.0fs)",
        settings.resource_pressure_sample_interval_seconds,
    )

    global _health_snapshot, _gallerydl_version
    try:
        _gallerydl_version = await asyncio.to_thread(_probe_gallerydl_version)
    except Exception:
        _gallerydl_version = None
    try:
        # Build once before the application starts accepting requests.  Every
        # health HTTP request can then remain a pure in-memory snapshot read.
        _health_snapshot = await _build_health_snapshot()
    except Exception:
        logger.warning("Initial health snapshot aggregation failed", exc_info=True)
        _health_snapshot = _starting_health_snapshot()

    async def health_aggregation_loop():
        global _health_snapshot
        while True:
            await asyncio.sleep(15)
            try:
                _health_snapshot = await _build_health_snapshot()
            except Exception:
                logger.warning("Health snapshot aggregation failed", exc_info=True)

    health_aggregation_task = asyncio.create_task(health_aggregation_loop())
    logger.info("System health snapshot aggregation started (15s)")

    yield

    # ── Cleanup ──
    ws_task.cancel()
    stale_task.cancel()
    operation_maintenance_task.cancel()
    import_recovery_task.cancel()
    download_dispatch_recovery_task.cancel()
    outbox_coordinator_task.cancel()
    memory_task.cancel()
    resource_pressure_task.cancel()
    health_aggregation_task.cancel()
    try:
        await ws_task
    except asyncio.CancelledError:
        pass
    try:
        await stale_task
    except asyncio.CancelledError:
        pass
    try:
        await operation_maintenance_task
    except asyncio.CancelledError:
        pass
    try:
        await import_recovery_task
    except asyncio.CancelledError:
        pass
    try:
        await download_dispatch_recovery_task
    except asyncio.CancelledError:
        pass
    try:
        await outbox_coordinator_task
    except asyncio.CancelledError:
        pass
    try:
        await memory_task
    except asyncio.CancelledError:
        pass
    try:
        await resource_pressure_task
    except asyncio.CancelledError:
        pass
    try:
        await health_aggregation_task
    except asyncio.CancelledError:
        pass
    await engine.dispose()
    logger.info("backend stopped")


def _process_rss_mb() -> float | None:
    """Current resident set size of this process in MB, or None if unavailable.
    Reads /proc/self/status (Linux/containers); this is what the OOM killer
    weighs against the container memory limit."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024  # kB -> MB
    except Exception:
        pass
    try:
        import resource
        # ru_maxrss is peak, in kB on Linux — fallback only.
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        return None


app = FastAPI(
    title="auto-gallery",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    generate_unique_id_function=stable_operation_id,
)


def _openapi():
    return build_openapi_schema(app)


app.openapi = _openapi

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)


@app.middleware("http")
async def foreground_latency_feedback(request: Request, call_next):
    """Feed interactive search latency into the shadow/AIMD controller.

    The hook is in-process and allocation-bounded; it never performs Redis or
    database I/O on the request path.  The resource monitor consumes the
    rolling p95 on its next sample and can reduce background grants when the
    gallery itself regresses.
    """

    started = time.perf_counter()
    try:
        return await call_next(request)
    finally:
        if request.method == "GET":
            try:
                from app.services.resource_pressure import (
                    record_foreground_latency,
                )

                record_foreground_latency(
                    request.url.path,
                    (time.perf_counter() - started) * 1000,
                )
            except Exception:
                logger.debug("Unable to record foreground latency", exc_info=True)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    safe_body = "<redacted>"
    raw = b""
    try:
        raw = await request.body()
        import json as _json
        data = _json.loads(raw[:2000])
        # Redact any key whose name contains a sensitive substring
        _sensitive_substrings = (
            "password", "token", "secret", "key", "credential", "cookie",
        )
        for key in list(data.keys()):
            if any(ss in key.lower() for ss in _sensitive_substrings):
                data[key] = "***REDACTED***"
        safe_body = str(data)
    except Exception:
        safe_body = f"<{len(raw)} bytes, parse error>"
    logger.warning("Validation error on %s %s: %s | body: %s",
                   request.method, request.url.path, exc.errors(), safe_body)
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(DanbooruUnavailableError)
async def danbooru_unavailable_handler(request: Request, exc: DanbooruUnavailableError):
    logger.warning("Danbooru unavailable during %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "Danbooru API is unreachable; try again later.",
                 "error_code": "danbooru_unavailable"},
    )


@app.exception_handler(QueueAdmissionError)
async def queue_admission_error_handler(request: Request, exc: QueueAdmissionError):
    """Expose Redis queue protection as an explicit, retryable degradation."""

    logger.warning(
        "Queue admission rejected during %s %s code=%s details=%s",
        request.method,
        request.url.path,
        exc.code,
        exc.details,
    )
    return JSONResponse(status_code=503, content={"detail": exc.payload()})

app.include_router(api_router)
app.include_router(media_router)
app.include_router(create_api_docs_router(app))


async def _service_readiness() -> dict[str, str]:
    services = {"postgres": "unknown", "redis": "unknown", "meilisearch": "unknown"}

    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
        services["postgres"] = "up"
    except Exception:
        services["postgres"] = "down"

    try:
        from app.services.resource_pressure import collect_redis_health

        redis_health = await asyncio.to_thread(collect_redis_health)
        services["redis"] = "up" if redis_health.get("writable") else "down"
    except Exception:
        services["redis"] = "down"

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.meili_url.rstrip('/')}/health",
                headers={"Authorization": f"Bearer {settings.meili_master_key}"},
                timeout=2,
            )
        services["meilisearch"] = "up" if resp.status_code == 200 else "down"
    except Exception:
        services["meilisearch"] = "down"

    return services


async def _resource_pressure_health() -> dict:
    """Build the additive, failure-isolated resource section for /health."""

    from app.services.resource_pressure import (
        collect_queue_worker_health,
        collect_redis_health,
        get_resource_pressure_snapshot,
        sample_cgroup_contribution,
    )

    try:
        pressure, redis_health, queue_worker, backend_cgroup = await asyncio.gather(
            get_resource_pressure_snapshot(),
            asyncio.to_thread(collect_redis_health),
            asyncio.to_thread(collect_queue_worker_health),
            asyncio.to_thread(sample_cgroup_contribution),
        )
    except Exception:
        logger.warning("Resource health collection failed", exc_info=True)
        pressure = {
            "status": "paused",
            "reasons": ["resource_health_unavailable"],
            "sampled_at": None,
            "memory": {"available_bytes": None, "total_bytes": None, "available_ratio": None},
            "swap": {"free_bytes": None, "total_bytes": None, "free_ratio": None},
            "psi": {"memory_full_avg10": None, "io_full_avg10": None},
        }
        redis_health = {
            "used_memory_bytes": None,
            "maxmemory_bytes": None,
            "usage_ratio": None,
            "writable": False,
            "rejected_writes": None,
            "oom_rejected_writes": None,
            "application_rejected_enqueues": None,
        }
        queue_worker = ({}, {})
        backend_cgroup = {
            "scope": "current_cgroup",
            "memory": {"current_bytes": None, "peak_bytes": None, "limit_bytes": None},
            "memory_events": {"max": None, "oom": None, "oom_kill": None},
        }

    queues, workers = queue_worker
    workers = dict(workers)
    queue_activity = workers.pop("queue_activity", {})
    pressure = dict(pressure)
    pressure["project_cgroup_contribution"] = {
        "scope": "auto_gallery_project_only",
        "backend": backend_cgroup,
        "workers": workers.get("cgroup_contribution") or {},
    }
    pressure_reasons = list(pressure.get("reasons") or [])
    operational_soft_reasons: list[str] = []
    operational_hard_reasons: list[str] = []

    # Surface every resource-protection condition through the same additive
    # health section. An unwritable project Redis is a hard dependency for
    # durable RQ admission, leases and CAS markers, so heavy work must fail
    # closed. Capacity warnings and worker supervision remain soft signals.
    redis_usage = redis_health.get("usage_ratio")
    if redis_health.get("writable") is False:
        pressure_reasons.append("redis_unwritable")
        operational_hard_reasons.append("redis_unwritable")
    if isinstance(redis_usage, (int, float)):
        if redis_usage >= 0.90:
            pressure_reasons.append("redis_memory_critical")
            operational_soft_reasons.append("redis_memory_critical")
        elif redis_usage >= 0.80:
            pressure_reasons.append("redis_memory_high")
            operational_soft_reasons.append("redis_memory_high")

    supervisors = workers.get("supervisors") or {}
    observed_worker_groups: set[str] = set()
    for supervisor in supervisors.values():
        if supervisor.get("stale"):
            continue
        supervisor_queues = [str(name) for name in (supervisor.get("queues") or [])]
        if any(name.startswith("downloads") for name in supervisor_queues):
            observed_worker_groups.add("downloads")
        for group in ("imports", "operations", "scheduled"):
            if group in supervisor_queues:
                observed_worker_groups.add(group)
        state = supervisor.get("state")
        worker_count = supervisor.get("worker_count")
        if state == "circuit_open":
            pressure_reasons.append("worker_circuit_open")
            operational_soft_reasons.append("worker_circuit_open")
        elif state in {"backoff", "stopping"} or (
            state is not None and isinstance(worker_count, int) and worker_count < 1
        ):
            pressure_reasons.append("worker_unavailable")
            operational_soft_reasons.append("worker_unavailable")
    missing_worker_groups = sorted(
        {"downloads", "imports", "operations", "scheduled"} - observed_worker_groups
    )
    if missing_worker_groups:
        missing_reason = f"worker_heartbeat_missing:{','.join(missing_worker_groups)}"
        pressure_reasons.append(missing_reason)
        operational_soft_reasons.append(missing_reason)

    pressure_reasons = list(dict.fromkeys(pressure_reasons))
    pressure["reasons"] = pressure_reasons
    pressure["hard_reasons"] = list(
        dict.fromkeys(
            [
                *(pressure.get("hard_reasons") or []),
                *operational_hard_reasons,
            ]
        )
    )
    pressure["soft_reasons"] = list(
        dict.fromkeys(
            [
                *(pressure.get("soft_reasons") or []),
                *operational_soft_reasons,
            ]
        )
    )
    if operational_hard_reasons:
        pressure["status"] = "paused"
        pressure["controller_mode"] = "critical"
        controller = pressure.setdefault("controller", {})
        controller["mode"] = "critical"
        controller["legacy_status"] = "paused"
        controller["hard_gate_active"] = True
        controller["effective_throughput_scale"] = 0.0
        budget = pressure.setdefault("budget", {})
        budget["effective_throughput_scale"] = 0.0
        budget["read_bytes_per_second"] = 0
        budget["write_bytes_per_second"] = 0
    elif pressure.get("status") == "normal" and pressure_reasons:
        pressure["status"] = "warning"
    configured = 3
    try:
        from app.services.settings import get_download_defaults

        async with async_session() as session:
            defaults = await get_download_defaults(session)
        configured = max(1, min(5, int(defaults.get("download_concurrency", 3))))
    except Exception:
        logger.debug("Download concurrency health fallback used", exc_info=True)
    from app.services.device_profile import current_device_profile

    device_profile = current_device_profile()
    cap = min(
        max(1, min(5, int(settings.download_concurrency_cap))),
        device_profile.download_concurrency_limit,
    )
    desired_effective = min(configured, cap)
    running_effective = None
    for supervisor in supervisors.values():
        if supervisor.get("stale"):
            continue
        supervisor_queues = supervisor.get("queues") or []
        if any(str(name).startswith("downloads") for name in supervisor_queues):
            try:
                running_effective = int(supervisor.get("effective_concurrency"))
            except (TypeError, ValueError):
                running_effective = None
            break

    return {
        **pressure,
        "redis": redis_health,
        "queues": queues,
        "queue_activity": queue_activity,
        "workers": workers,
        "download_concurrency": {
            "configured": configured,
            "cap": cap,
            "device_profile": device_profile.name,
            # A live supervisor heartbeat is the authoritative actual value.
            # Fall back to the deployment calculation during startup or while
            # upgrading from an image that predates supervisor telemetry.
            "effective": running_effective if running_effective is not None else desired_effective,
            "desired_effective": desired_effective,
            "restart_required": (
                running_effective is not None
                and running_effective != desired_effective
            ),
        },
    }


_readiness_cache: dict | None = None
_readiness_cache_ts: float = 0.0
_READINESS_CACHE_TTL = 15.0
_health_snapshot: dict | None = None
_gallerydl_version: str | None = None


def _starting_health_snapshot() -> dict:
    return {
        "status": "degraded",
        "version": "0.1.0",
        "services": {
            "postgres": "unknown",
            "redis": "unknown",
            "meilisearch": "unknown",
        },
        "disk": "unknown",
        "business": {
            "queues": {},
            "jobs": {},
            "scheduler": {},
            "gallerydl": {},
            "outboxes": {},
        },
        "resource_pressure": {
            "status": "paused",
            "controller_mode": "critical",
            "reasons": ["health_snapshot_starting"],
            "hard_reasons": ["health_snapshot_starting"],
            "soft_reasons": [],
        },
    }


def _probe_gallerydl_version() -> str | None:
    import subprocess

    try:
        result = subprocess.run(
            ["gallery-dl", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return (result.stdout or result.stderr or "").strip() or None
    except Exception:
        return None


@app.get("/api/v1/system/ready")
async def ready():
    global _readiness_cache, _readiness_cache_ts
    now_mono = time.monotonic()
    if _readiness_cache is not None and (now_mono - _readiness_cache_ts) < _READINESS_CACHE_TTL:
        services = _readiness_cache
    else:
        services = await _service_readiness()
        _readiness_cache = services
        _readiness_cache_ts = now_mono
    required_up = services["postgres"] == "up" and services["redis"] == "up"
    all_up = all(v == "up" for v in services.values())
    return JSONResponse(
        status_code=200 if required_up else 503,
        content={
            "status": "ok" if all_up else "degraded" if required_up else "unavailable",
            "services": services,
        },
    )


async def _build_health_snapshot() -> dict:
    services = await _service_readiness()

    # Disk space check (cached)
    disk = "unknown"
    try:
        import shutil
        for path, label in [(settings.download_root, "downloads"), (settings.library_root, "library")]:
            usage = shutil.disk_usage(path)
            pct = usage.free / usage.total
            if pct < 0.05:
                disk = "critical"
                break
            elif pct < 0.10:
                disk = "warning"
        if disk == "unknown":
            disk = "ok"
    except Exception:
        disk = "unknown"

    resource_pressure = await _resource_pressure_health()
    business = {
        "queues": {},
        "jobs": {},
        "scheduler": {},
        "gallerydl": {},
        "outboxes": {},
    }
    try:
        from app.services.redis_client import get_redis
        from rq import Queue
        from rq.registry import ScheduledJobRegistry

        r = get_redis()
        default_q = Queue(connection=r)
        scheduled_q = Queue(name="scheduled", connection=r)
        scheduled_reg = ScheduledJobRegistry(queue=scheduled_q)
        business["queues"] = {
            "default": len(default_q),
            "scheduled": len(scheduled_q),
            "scheduled_registry": scheduled_reg.count,
        }
        scheduled_ids = scheduled_reg.get_job_ids()
        business["scheduler"] = {
            # New scheduler jobs use a deterministic prefix.  Inspecting IDs
            # avoids fetching and deserializing every scheduled RQ job during
            # each 15-second health aggregation.
            "sync_scheduled": str(
                any(str(job_id).startswith("subscription-sync:") for job_id in scheduled_ids)
            ).lower(),
        }
    except Exception:
        logger.debug("Business queue health skipped", exc_info=True)

    try:
        from sqlalchemy import func, select
        from app.models import DownloadJob, ImportJob, SubscriptionSource

        async with async_session() as session:
            download_counts = await session.execute(
                select(DownloadJob.status, func.count(DownloadJob.id)).group_by(DownloadJob.status)
            )
            import_counts = await session.execute(
                select(ImportJob.status, func.count(ImportJob.id)).group_by(ImportJob.status)
            )
            auth_unhealthy = await session.execute(
                select(func.count(SubscriptionSource.id)).where(SubscriptionSource.auth_healthy == False)
            )
        business["jobs"] = {
            "downloads": {status: count for status, count in download_counts.all()},
            "imports": {status: count for status, count in import_counts.all()},
            "auth_unhealthy_sources": auth_unhealthy.scalar() or 0,
        }
        from app.services.outbox_coordinator import outbox_health

        async with async_session() as session:
            business["outboxes"] = await outbox_health(session)
    except Exception:
        logger.debug("Business DB health skipped", exc_info=True)

    try:
        import json
        config_path = os.path.join(settings.gallerydl_config_root, "config.json")
        config_readable = True
        if os.path.exists(config_path):
            with open(config_path) as f:
                json.load(f)
        business["gallerydl"] = {
            "version": _gallerydl_version,
            "config_readable": config_readable,
        }
    except Exception:
        business["gallerydl"] = {"version": None, "config_readable": False}

    all_up = all(v == "up" for v in services.values())
    protection_normal = resource_pressure.get("status") == "normal"
    return {
        "status": "ok" if all_up and disk == "ok" and protection_normal else "degraded",
        "version": "0.1.0",
        "services": services,
        "disk": disk,
        "business": business,
        "resource_pressure": resource_pressure,
    }


@app.get("/api/v1/system/health")
async def health():
    # The background aggregator owns all database/Redis/cgroup work. A request
    # never starts gallery-dl, scans registries or competes with the foreground
    # works page for connections.
    if _health_snapshot is not None:
        return _health_snapshot
    return _starting_health_snapshot()
