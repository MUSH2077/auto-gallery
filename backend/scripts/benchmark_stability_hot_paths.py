#!/usr/bin/env python3
"""Measure stability hot paths without ever mixing fixture and live modes.

Synthetic mode inserts the release acceptance shape into one transaction and
always rolls it back.  It is intentionally guarded by explicit command-line
confirmation and, by default, a loopback-only database URL.

Live mode starts with ``SET TRANSACTION READ ONLY`` and disables Gitllery's
Redis response-cache writes.  It measures current rows only; it never builds,
verifies, promotes, or settles a Gitllery generation.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
from unittest.mock import patch
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import event, func, select, text
from sqlalchemy.engine import make_url

from app.config import settings
from app.database import async_session, engine
from app.models import Asset, GitlleryProjectionOutbox, ImportJob, StorageArtifact
from app.services.asset_reconciliation import AssetReconciliation
from app.services.gitllery.service import GitlleryService
from app.services.outbox_coordinator import outbox_readiness


DEFAULT_INTENT_COUNT = 70_000
DEFAULT_ASSET_COUNT = 100_000
DEFAULT_REPOSITORY_COUNT = 804
PERFORMANCE_TARGETS_MS = {
    "outbox_readiness": 50.0,
    "dedup_candidates": 100.0,
    "import_job_delete": 200.0,
    "workbench_cached": 500.0,
    "gitllery_status": 200.0,
}
LOOPBACK_HOSTS = {None, "", "localhost", "127.0.0.1", "::1"}


def percentile(values: list[float], rank: float) -> float:
    """Return the nearest-rank percentile used by the acceptance gates."""

    if not values:
        raise ValueError("percentile requires at least one sample")
    if not 0 < rank <= 1:
        raise ValueError("percentile rank must be in (0, 1]")
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * rank) - 1))
    return float(ordered[position])


def assert_synthetic_database_allowed(
    database_url: str,
    *,
    confirmed: bool,
    allow_non_loopback: bool,
) -> None:
    """Fail closed before creating a large synthetic workload."""

    if not confirmed:
        raise RuntimeError("synthetic mode requires --confirm-disposable")
    parsed = make_url(database_url)
    if parsed.database in {None, "", "postgres", "template0", "template1"}:
        raise RuntimeError("synthetic mode requires a named disposable application database")
    if parsed.host not in LOOPBACK_HOSTS and not allow_non_loopback:
        raise RuntimeError(
            "non-loopback synthetic databases require "
            "--allow-non-loopback-disposable"
        )


async def enforce_read_only_transaction(db) -> None:
    """Make the current transaction fail closed on every database write."""

    await db.execute(text("SET TRANSACTION READ ONLY"))


async def _timed(call: Callable[[], Awaitable[Any]]) -> tuple[float, Any]:
    started = time.perf_counter()
    value = await call()
    return (time.perf_counter() - started) * 1000, value


async def _measure(
    call: Callable[[], Awaitable[Any]],
    *,
    repeats: int,
    warmups: int = 1,
) -> tuple[list[float], Any]:
    value: Any = None
    for _ in range(max(0, warmups)):
        value = await call()
    samples: list[float] = []
    for _ in range(max(1, repeats)):
        elapsed, value = await _timed(call)
        samples.append(elapsed)
    return samples, value


def _measurement(
    name: str,
    samples: list[float],
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = PERFORMANCE_TARGETS_MS[name]
    p95 = percentile(samples, 0.95)
    return {
        "samples": len(samples),
        "p50_ms": round(percentile(samples, 0.50), 3),
        "p95_ms": round(p95, 3),
        "max_ms": round(max(samples), 3),
        "target_ms": target,
        "passed": p95 < target,
        **(details or {}),
    }


def _skipped(reason: str) -> dict[str, Any]:
    return {"skipped": True, "reason": reason, "passed": None}


@contextmanager
def _gitllery_response_cache(*, memory: bool):
    """Keep live measurements off Redis while preserving cache semantics."""

    values: dict[str, Any] = {}

    def get(key: str):
        return values.get(key) if memory else None

    def set_value(key: str, value: Any, _ttl_seconds: int = 0):
        if memory:
            values[key] = value

    with (
        patch("app.services.cache.cache_get", side_effect=get),
        patch("app.services.cache.cache_set", side_effect=set_value),
    ):
        yield


@contextmanager
def _capture_sql_timings():
    """Capture statement timings without parameters or row data."""

    timings: list[tuple[str, float]] = []

    def before(_conn, _cursor, statement, _params, context, _many):
        context._stability_started_at = time.perf_counter()
        context._stability_statement = " ".join(statement.split()[:12])

    def after(_conn, _cursor, _statement, _params, context, _many):
        started = getattr(context, "_stability_started_at", None)
        if started is not None:
            timings.append(
                (
                    getattr(context, "_stability_statement", "unknown"),
                    (time.perf_counter() - started) * 1000,
                )
            )

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    event.listen(engine.sync_engine, "after_cursor_execute", after)
    try:
        yield timings
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)
        event.remove(engine.sync_engine, "after_cursor_execute", after)


def _slow_sql_summary(timings: list[tuple[str, float]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[float]] = {}
    for statement, elapsed in timings:
        grouped.setdefault(statement, []).append(elapsed)
    return [
        {
            "statement": statement,
            "calls": len(samples),
            "max_ms": round(max(samples), 3),
            "total_ms": round(sum(samples), 3),
        }
        for statement, samples in sorted(
            grouped.items(),
            key=lambda item: sum(item[1]),
            reverse=True,
        )[:5]
    ]


def _reset_workbench_cache() -> None:
    from app.api import system as system_api

    system_api._workbench_cache = None
    system_api._workbench_cache_ts = 0.0
    system_api._workbench_cache_actor = None
    system_api._workbench_cache_generation = None


async def _measure_workbench_cache(db, *, repeats: int) -> dict[str, Any]:
    from app.api import system as system_api

    _reset_workbench_cache()
    user = SimpleNamespace(id=1, is_admin=True, permissions=["system", "tasks"])

    async def load():
        return await system_api.workbench_summary(
            refresh=False,
            user=user,
            db=db,
        )

    cold_ms, _ = await _timed(load)
    samples, payload = await _measure(load, repeats=repeats, warmups=0)
    _reset_workbench_cache()
    return _measurement(
        "workbench_cached",
        samples,
        details={
            "cold_ms": round(cold_ms, 3),
            "auth_actionable_count": int(
                ((payload or {}).get("attention") or {}).get(
                    "auth_actionable_count",
                    0,
                )
            ),
        },
    )


async def _seed_synthetic_shape(
    db,
    *,
    run_key: str,
    asset_count: int,
    intent_count: int,
    repository_count: int,
) -> dict[str, UUID]:
    """Insert a deterministic large shape into the caller's transaction."""

    await db.execute(
        text(
            """
            INSERT INTO works (
                id, title, is_nsfw, is_ai_generated, is_favorite
            )
            SELECT
                md5(:run_key || '-work-' || series)::uuid,
                'stability benchmark work ' || series,
                false,
                false,
                false
            FROM generate_series(1, :repository_count) AS series
            """
        ),
        {"run_key": run_key, "repository_count": repository_count},
    )
    await db.execute(
        text(
            """
            INSERT INTO work_sources (
                id, work_id, source, source_work_id, source_creator_id,
                source_url, raw_metadata
            )
            SELECT
                md5(:run_key || '-work-source-' || series)::uuid,
                md5(:run_key || '-work-' || series)::uuid,
                CASE WHEN series % 2 = 0 THEN 'danbooru' ELSE 'pixiv' END,
                :run_key || '-work-' || series,
                :run_key || '-repo-' || series,
                'https://benchmark.invalid/work/' || series,
                jsonb_build_object('id', :run_key || '-creator-' || series)
            FROM generate_series(1, :repository_count) AS series
            """
        ),
        {"run_key": run_key, "repository_count": repository_count},
    )
    await db.execute(
        text(
            """
            INSERT INTO assets (
                id, file_path, file_name, file_size, mime_type, width, height,
                sha256, phash, phash_version
            )
            SELECT
                md5(:run_key || '-asset-' || series)::uuid,
                :run_key || '/asset-' || series || '.jpg',
                'asset-' || series || '.jpg',
                100000 + series,
                'image/jpeg',
                2048,
                2048,
                md5(:run_key || '-sha-' || series),
                CASE
                    WHEN series = 1 THEN '0123456789abcdef'
                    WHEN series % 1000 = 0
                        THEN '012' || substr(md5(:run_key || '-phash-' || series), 4, 13)
                    ELSE substr(md5(:run_key || '-phash-' || series), 1, 16)
                END,
                'imagehash-phash-v1'
            FROM generate_series(1, :asset_count) AS series
            """
        ),
        {"run_key": run_key, "asset_count": asset_count},
    )
    await db.execute(
        text(
            """
            INSERT INTO asset_sources (
                id, asset_id, work_source_id, source, source_asset_id,
                source_url, ordinal, role
            )
            SELECT
                md5(:run_key || '-asset-source-' || series)::uuid,
                md5(:run_key || '-asset-' || series)::uuid,
                md5(
                    :run_key || '-work-source-' ||
                    (((series - 1) % :repository_count) + 1)
                )::uuid,
                CASE
                    WHEN (((series - 1) % :repository_count) + 1) % 2 = 0
                        THEN 'danbooru'
                    ELSE 'pixiv'
                END,
                :run_key || '-asset-' || series,
                'https://benchmark.invalid/asset/' || series,
                ((series - 1) / :repository_count)::integer,
                'page'
            FROM generate_series(1, :asset_count) AS series
            """
        ),
        {
            "run_key": run_key,
            "asset_count": asset_count,
            "repository_count": repository_count,
        },
    )

    # The installed trigger creates one durable Gitllery intent for every
    # curation commit.  This exercises the actual 70k-intent table shape.
    await db.execute(
        text(
            """
            INSERT INTO curation_commits (
                id, actor_type, actor_id, message, trigger, occurred_at,
                status, stats, metadata, created_at, updated_at
            )
            SELECT
                md5(:run_key || '-commit-' || series)::uuid,
                'benchmark',
                :run_key,
                'stability benchmark commit ' || series,
                'stability-benchmark',
                now() + series * interval '1 microsecond',
                'active',
                jsonb_build_object('changes', 0),
                jsonb_build_object('benchmark', true),
                now() + series * interval '1 microsecond',
                now() + series * interval '1 microsecond'
            FROM generate_series(1, :intent_count) AS series
            """
        ),
        {"run_key": run_key, "intent_count": intent_count},
    )
    await db.execute(
        text(
            """
            INSERT INTO gitllery_repository_state (
                id, repository_key, source, creator_dir, product_version,
                format_id, format_revision, mode, generation, segment_count,
                commit_count, change_count
            )
            SELECT
                md5(:run_key || '-repo-state-' || series)::uuid,
                (CASE WHEN series % 2 = 0 THEN 'danbooru:' ELSE 'pixiv:' END)
                    || :run_key || '-repo-' || series,
                CASE WHEN series % 2 = 0 THEN 'danbooru' ELSE 'pixiv' END,
                :run_key || '-creator-' || series,
                'v1',
                'gitllery-segment',
                1,
                'shadow',
                :run_key,
                0,
                0,
                0
            FROM generate_series(1, :repository_count) AS series
            """
        ),
        {"run_key": run_key, "repository_count": repository_count},
    )

    # Use ORM defaults for the small parent hierarchy so the fixture tracks
    # future non-null task fields without duplicating their defaults here.
    from app.models import Creator, DownloadJob, ImportJob, Subscription

    creator = Creator(name=f"{run_key}-creator", is_active=False)
    db.add(creator)
    await db.flush()
    subscription = Subscription(
        creator_id=creator.id,
        name=f"{run_key}-subscription",
        is_active=False,
        sync_enabled=False,
        schedule_mode="manual",
    )
    db.add(subscription)
    await db.flush()
    download = DownloadJob(
        subscription_id=subscription.id,
        source="benchmark",
        source_url="https://benchmark.invalid/download",
        status="complete",
    )
    db.add(download)
    await db.flush()
    import_job = ImportJob(download_job_id=download.id, status="complete")
    db.add(import_job)
    await db.flush()
    await db.execute(
        text(
            """
            INSERT INTO storage_artifacts (
                id, storage_root, file_path, source, creator_dir,
                source_work_id, file_name, artifact_type, file_size, mtime_ns,
                download_job_id, import_job_id, state, attempts
            )
            SELECT
                md5(:run_key || '-artifact-' || series)::uuid,
                'downloads',
                :run_key || '/artifact-' || series || '.jpg',
                'benchmark',
                :run_key,
                :run_key || '-work-' || series,
                'artifact-' || series || '.jpg',
                'media',
                100000 + series,
                series,
                :download_job_id,
                CASE
                    WHEN series <= 500 THEN CAST(:import_job_id AS uuid)
                    ELSE NULL::uuid
                END,
                'imported',
                0
            FROM generate_series(1, :asset_count) AS series
            """
        ),
        {
            "run_key": run_key,
            "asset_count": asset_count,
            "download_job_id": download.id,
            "import_job_id": import_job.id,
        },
    )
    await db.flush()
    await db.execute(
        text(
            "ANALYZE assets, asset_sources, work_sources, curation_commits, "
            "gitllery_projection_outbox, gitllery_repository_state, "
            "storage_artifacts"
        )
    )
    return {
        "asset_id": UUID(hex=hashlib.md5(
            f"{run_key}-asset-1".encode(),
            usedforsecurity=False,
        ).hexdigest()),
        "import_job_id": import_job.id,
    }


async def _measure_delete_with_rollback(
    db,
    import_job_id: UUID,
    *,
    repeats: int,
    warmups: int,
) -> list[float]:
    samples: list[float] = []
    total = max(0, warmups) + max(1, repeats)
    for index in range(total):
        savepoint = f"stability_delete_{index}"
        await db.execute(text(f"SAVEPOINT {savepoint}"))
        started = time.perf_counter()
        await db.execute(
            text("DELETE FROM import_jobs WHERE id = :job_id"),
            {"job_id": import_job_id},
        )
        elapsed = (time.perf_counter() - started) * 1000
        await db.execute(text(f"ROLLBACK TO SAVEPOINT {savepoint}"))
        await db.execute(text(f"RELEASE SAVEPOINT {savepoint}"))
        if index >= warmups:
            samples.append(elapsed)
    return samples


async def _measure_common_reads(
    db,
    *,
    anchor: Asset | None,
    repeats: int,
    cache_gitllery_status: bool = False,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    readiness_samples, readiness = await _measure(
        lambda: outbox_readiness(db),
        repeats=repeats,
    )
    results["outbox_readiness"] = _measurement(
        "outbox_readiness",
        readiness_samples,
        details={"ready_queues": readiness},
    )

    if anchor is None:
        results["dedup_candidates"] = _skipped(
            "no image asset with pHash and bound source context"
        )
    else:
        reconciliation = AssetReconciliation(db)
        candidate_samples, candidates = await _measure(
            lambda: reconciliation._candidate_assets(anchor),
            repeats=repeats,
        )
        results["dedup_candidates"] = _measurement(
            "dedup_candidates",
            candidate_samples,
            details={"candidate_count": len(candidates)},
        )

    with (
        _gitllery_response_cache(memory=cache_gitllery_status),
        _capture_sql_timings() as sql_timings,
    ):
        if cache_gitllery_status:
            cold_status_ms, status = await _timed(lambda: GitlleryService(db).status())
            status_samples, status = await _measure(
                lambda: GitlleryService(db).status(),
                repeats=repeats,
                warmups=0,
            )
        else:
            cold_status_ms = None
            status_samples, status = await _measure(
                lambda: GitlleryService(db).status(),
                repeats=repeats,
            )
    results["gitllery_status"] = _measurement(
        "gitllery_status",
        status_samples,
        details={
            "repository_count": len(status.get("repositories") or []),
            "unplanned_intents": int(status.get("unplanned_intents") or 0),
            "projection_state": status.get("projection_state"),
            "cache_mode": "in-process-read-only" if cache_gitllery_status else "disabled",
            "cold_database_ms": (
                round(cold_status_ms, 3) if cold_status_ms is not None else None
            ),
            "sql_calls_including_warmup": len(sql_timings),
            "slow_sql": _slow_sql_summary(sql_timings),
        },
    )
    results["workbench_cached"] = await _measure_workbench_cache(
        db,
        repeats=repeats,
    )
    return results


async def run_synthetic(args) -> dict[str, Any]:
    assert_synthetic_database_allowed(
        settings.database_url,
        confirmed=args.confirm_disposable,
        allow_non_loopback=args.allow_non_loopback_disposable,
    )
    if settings.gitllery_projection_mode.strip().lower() != "shadow":
        raise RuntimeError("synthetic acceptance requires GITLLERY_PROJECTION_MODE=shadow")

    run_key = f"stability-{uuid4().hex}"
    async with async_session() as db:
        transaction = await db.begin()
        try:
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_name))"),
                {"lock_name": "auto-gallery-stability-performance"},
            )
            print(
                "seeding disposable stability fixture; the transaction will be rolled back",
                file=sys.stderr,
            )
            ids = await _seed_synthetic_shape(
                db,
                run_key=run_key,
                asset_count=args.assets,
                intent_count=args.intents,
                repository_count=args.repositories,
            )
            anchor = await db.get(Asset, ids["asset_id"])
            measurements = await _measure_common_reads(
                db,
                anchor=anchor,
                repeats=args.repeats,
            )
            delete_samples = await _measure_delete_with_rollback(
                db,
                ids["import_job_id"],
                repeats=args.repeats,
                warmups=1,
            )
            measurements["import_job_delete"] = _measurement(
                "import_job_delete",
                delete_samples,
                details={"linked_artifacts": 500},
            )
            fixture_intents = int(
                (
                    await db.execute(
                        select(func.count(GitlleryProjectionOutbox.id)).where(
                            GitlleryProjectionOutbox.commit_id.in_(
                                select(text("id"))
                                .select_from(text("curation_commits"))
                                .where(text("actor_id = :run_key"))
                            )
                        ),
                        {"run_key": run_key},
                    )
                ).scalar_one()
                or 0
            )
            payload = {
                "mode": "synthetic-rollback",
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "scale": {
                    "assets": args.assets,
                    "storage_artifacts": args.assets,
                    "gitllery_intents": fixture_intents,
                    "repositories": args.repositories,
                },
                "measurements": measurements,
            }
        finally:
            if transaction.is_active:
                await transaction.rollback()
    return _finalize(payload)


async def _live_anchor(db) -> Asset | None:
    return (
        await db.execute(
            select(Asset)
            .where(
                Asset.mime_type.in_(
                    ("image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp")
                ),
                Asset.phash.is_not(None),
                Asset.phash != "",
                Asset.id.in_(
                    select(text("asset_id")).select_from(text("asset_sources"))
                ),
            )
            .order_by(Asset.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _measure_live_delete_plan(db, *, repeats: int) -> dict[str, Any]:
    import_job_id = (
        await db.execute(select(ImportJob.id).order_by(ImportJob.created_at.desc()).limit(1))
    ).scalar_one_or_none()
    sampled_existing_job = import_job_id is not None
    if import_job_id is None:
        # EXPLAIN does not execute the delete.  A sentinel still validates the
        # FK lookup plan when operational history has already been compacted.
        import_job_id = UUID(int=0)

    async def explain():
        artifact_plan = (
            await db.execute(
                text(
                    "EXPLAIN (FORMAT JSON) SELECT id FROM storage_artifacts "
                    "WHERE import_job_id = :job_id"
                ),
                {"job_id": import_job_id},
            )
        ).scalar_one()
        delete_plan = (
            await db.execute(
                text(
                    "EXPLAIN (FORMAT JSON) DELETE FROM import_jobs "
                    "WHERE id = :job_id"
                ),
                {"job_id": import_job_id},
            )
        ).scalar_one()
        return artifact_plan, delete_plan

    samples, plans = await _measure(explain, repeats=repeats)
    index_present = bool(
        (
            await db.execute(
                text(
                    "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() "
                    "AND tablename = 'storage_artifacts' "
                    "AND indexname = 'ix_storage_artifacts_import_job_id'"
                )
            )
        ).scalar_one_or_none()
    )
    artifact_plan, delete_plan = plans
    return _measurement(
        "import_job_delete",
        samples,
        details={
            "measurement_kind": "read_only_explain",
            "sampled_existing_job": sampled_existing_job,
            "index_present": index_present,
            "index_used": "ix_storage_artifacts_import_job_id" in json.dumps(artifact_plan),
            "delete_plan_node": (delete_plan[0].get("Plan") or {}).get("Node Type"),
        },
    )


async def run_live_read_only(args) -> dict[str, Any]:
    if settings.gitllery_projection_mode.strip().lower() != "shadow":
        raise RuntimeError("live measurement refuses to run unless Gitllery is shadow")

    async with async_session() as db:
        await enforce_read_only_transaction(db)
        scale_row = (
            await db.execute(
                select(
                    select(func.count()).select_from(Asset).scalar_subquery(),
                    select(func.count())
                    .select_from(StorageArtifact)
                    .scalar_subquery(),
                    select(func.count())
                    .select_from(GitlleryProjectionOutbox)
                    .where(GitlleryProjectionOutbox.state != "complete")
                    .scalar_subquery(),
                )
            )
        ).one()
        anchor = await _live_anchor(db)
        measurements = await _measure_common_reads(
            db,
            anchor=anchor,
            repeats=args.repeats,
            cache_gitllery_status=True,
        )
        measurements["import_job_delete"] = await _measure_live_delete_plan(
            db,
            repeats=args.repeats,
        )
        payload = {
            "mode": "live-read-only",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "scale": {
                "assets": int(scale_row[0] or 0),
                "storage_artifacts": int(scale_row[1] or 0),
                "pending_gitllery_intents": int(scale_row[2] or 0),
            },
            "measurements": measurements,
            "safety": {
                "database_transaction": "read-only",
                "gitllery_cache_writes": "disabled",
                "gitllery_build_or_verify": "not invoked",
            },
        }
        await db.rollback()
    return _finalize(payload)


def _finalize(payload: dict[str, Any]) -> dict[str, Any]:
    evaluated = [
        item.get("passed")
        for item in payload["measurements"].values()
        if item.get("passed") is not None
    ]
    payload["all_measured_targets_passed"] = bool(evaluated) and all(evaluated)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("synthetic", "live-read-only"),
        required=True,
    )
    parser.add_argument("--assets", type=int, default=DEFAULT_ASSET_COUNT)
    parser.add_argument("--intents", type=int, default=DEFAULT_INTENT_COUNT)
    parser.add_argument("--repositories", type=int, default=DEFAULT_REPOSITORY_COUNT)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--confirm-disposable", action="store_true")
    parser.add_argument("--allow-non-loopback-disposable", action="store_true")
    parser.add_argument("--enforce-targets", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


async def _main_async(args) -> int:
    try:
        if args.assets < 1 or args.intents < 1 or args.repositories < 1 or args.repeats < 1:
            raise RuntimeError("scale and repeat arguments must be positive")
        payload = (
            await run_synthetic(args)
            if args.mode == "synthetic"
            else await run_live_read_only(args)
        )
        rendered = json.dumps(payload, indent=2, sort_keys=True)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(f"{rendered}\n")
        print(rendered)
        if args.enforce_targets and not payload["all_measured_targets_passed"]:
            return 1
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    args = _parser().parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
