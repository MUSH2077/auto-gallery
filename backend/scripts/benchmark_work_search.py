#!/usr/bin/env python3
"""Validate bounded work-list latency, SQL fan-out, scale, and count dogpiles.

This script is intentionally opt-in and read-mostly.  Its only mutations are
short-lived Redis count-cache generations/keys used to create deterministic
cold and stale single-flight rounds; it never rebuilds an index or runs
``EXPLAIN ANALYZE``.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select

from app.database import async_session
from app.models import Asset, AssetSource, StorageArtifact, Work, WorkSourceTag, WorkTag
from app.services.cache import cache_bump_generation, cache_key
from app.services.redis_client import get_redis
from app.services.search import SearchService, _count_locks
from app.services.search_language import parse_search_query
from app.services.stage_metrics import measure_stage


NEW_SORT_TARGET_MS = 500.0
MAX_EXISTING_SORT_REGRESSION = 0.10
WORK_SEARCH_SQL_BUDGET = 6
EXISTING_SORT_CASES = frozenset(
    {
        "default",
        "created-desc",
        "created-asc",
        "posted-desc",
        "posted-asc",
        "updated-desc",
        "updated-asc",
        "title-desc",
        "title-asc",
    }
)


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    query: str
    seed: int | None = None
    cursor_page: bool = False


BENCHMARK_CASES = (
    BenchmarkCase("default", "", cursor_page=True),
    BenchmarkCase("created-desc", "sort:created-desc"),
    BenchmarkCase("created-asc", "sort:created-asc"),
    BenchmarkCase("posted-desc", "sort:posted-desc"),
    BenchmarkCase("posted-asc", "sort:posted-asc"),
    BenchmarkCase("updated-desc", "sort:updated-desc"),
    BenchmarkCase("updated-asc", "sort:updated-asc"),
    BenchmarkCase("title-desc", "is:sfw sort:title-desc"),
    BenchmarkCase("title-asc", "is:sfw sort:title-asc"),
    BenchmarkCase("source", "source:pixiv"),
    BenchmarkCase("favorite", "is:favorite"),
    BenchmarkCase("multi_asset", "has:multiple-assets"),
    BenchmarkCase("heat", "sort:heat-desc", cursor_page=True),
    BenchmarkCase(
        "random",
        "sort:random",
        seed=1_234_567_890,
        cursor_page=True,
    ),
)

SCALE_REQUIREMENTS = {
    "works": 70_000,
    "assets": 90_000,
    "tag_relations": 470_000,
    "artifacts": 330_000,
}


def existing_sort_regressions(
    current: dict[str, float],
    baseline: dict[str, float],
    *,
    maximum: float = MAX_EXISTING_SORT_REGRESSION,
) -> dict[str, dict[str, float]]:
    """Return existing-sort measurements that exceed the release baseline."""

    failures: dict[str, dict[str, float]] = {}
    for key, baseline_ms in baseline.items():
        if key.split(":", 1)[0] not in EXISTING_SORT_CASES or key not in current:
            continue
        current_ms = current[key]
        if current_ms > baseline_ms * (1 + maximum):
            failures[key] = {
                "baseline_ms": float(baseline_ms),
                "current_ms": float(current_ms),
            }
    return failures


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[
        max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    ]


async def _scale_snapshot(db) -> dict[str, int]:
    row = (await db.execute(select(
        select(func.count()).select_from(Work).scalar_subquery().label("works"),
        select(func.count()).select_from(Asset).scalar_subquery().label("assets"),
        (
            select(func.count()).select_from(WorkTag).scalar_subquery()
            + select(func.count()).select_from(WorkSourceTag).scalar_subquery()
        ).label("tag_relations"),
        select(func.count()).select_from(AssetSource).scalar_subquery().label("asset_links"),
        select(func.count()).select_from(StorageArtifact).scalar_subquery().label("artifacts"),
    ))).one()
    return {
        "works": int(row.works),
        "assets": int(row.assets),
        "tag_relations": int(row.tag_relations),
        "asset_links": int(row.asset_links),
        "artifacts": int(row.artifacts),
    }


async def _sample_search(
    service: SearchService,
    *,
    case: str,
    query: str,
    offset: int,
    cursor: str | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    metrics: dict[str, Any]
    with measure_stage(
        "benchmark_work_search",
        case=case,
        offset="seek" if cursor else offset,
    ) as metrics:
        started = time.perf_counter()
        await service.search(
            query,
            offset,
            30,
            scope="works",
            permissions={"library", "curation"},
            cursor=cursor,
            seed=seed,
        )
        latency_ms = (time.perf_counter() - started) * 1000
    return {**metrics, "latency_ms": latency_ms}


async def _measure(
    service: SearchService,
    *,
    case: str,
    query: str,
    offset: int,
    repeats: int,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    # Warm query plans, relation pages, and the exact-count cache outside the
    # measured window.
    await service.search(
        query,
        offset,
        30,
        scope="works",
        permissions={"library", "curation"},
        seed=seed,
    )
    return [
        await _sample_search(
            service,
            case=case,
            query=query,
            offset=offset,
            seed=seed,
        )
        for _ in range(repeats)
    ]


def _print_samples(
    *,
    case: str,
    offset: int | str,
    samples: list[dict[str, Any]],
    target_ms: float,
) -> tuple[float, int]:
    latencies = [float(sample["latency_ms"]) for sample in samples]
    p50 = _percentile(latencies, 0.50)
    p95 = _percentile(latencies, 0.95)
    max_sql = max(int(sample["sql_count"]) for sample in samples)
    rss_peak_mib = max(int(sample["rss_peak_bytes"]) for sample in samples) / 1024 / 1024
    cpu_ms = sum(float(sample["cpu_seconds"]) for sample in samples) * 1000
    read_bytes = sum(int(sample.get("read_bytes") or 0) for sample in samples)
    print(
        f"phase=work_list case={case} offset={offset} samples={len(samples)} "
        f"p50_ms={p50:.1f} p95_ms={p95:.1f} max_sql={max_sql} "
        f"cpu_total_ms={cpu_ms:.1f} read_bytes={read_bytes} "
        f"rss_peak_mib={rss_peak_mib:.1f} target_ms={target_ms:.1f}"
    )
    return p95, max_sql


async def _count_once(query, *, force_sfw: bool) -> int:
    async with async_session() as db:
        service = SearchService(db)
        conditions, _visibility, _has_tags = service._work_filter_conditions(
            query,
            {},
            force_sfw=force_sfw,
        )
        return await service._cached_work_total(
            select(Work).where(*conditions),
            query,
            force_sfw=force_sfw,
        )


async def _count_dogpile_round(
    *,
    mode: str,
    concurrency: int,
    delete_stale: bool,
) -> dict[str, Any]:
    query = parse_search_query("sort:created-desc", "works")
    generation = await asyncio.to_thread(cache_bump_generation, "works")
    total_key = cache_key(
        "works:count",
        query=query.canonical,
        force_sfw=False,
        generation=generation,
    )
    stale_key = cache_key(
        "works:count-stale",
        query=query.canonical,
        force_sfw=False,
    )

    def _clear_round_keys() -> None:
        redis = get_redis()
        keys = [total_key]
        if delete_stale:
            keys.append(stale_key)
        redis.delete(*keys)

    await asyncio.to_thread(_clear_round_keys)
    _count_locks.clear()
    metrics: dict[str, Any]
    with measure_stage(
        "benchmark_work_count_singleflight",
        mode=mode,
        concurrency=concurrency,
    ) as metrics:
        started = time.perf_counter()
        totals = await asyncio.gather(*(
            _count_once(query, force_sfw=False)
            for _ in range(concurrency)
        ))
        latency_ms = (time.perf_counter() - started) * 1000
    if len(set(totals)) != 1:
        raise RuntimeError(f"Count single-flight returned divergent totals: {totals}")
    payload = {
        **metrics,
        "latency_ms": latency_ms,
        "total": totals[0],
    }
    print(
        f"phase=count_singleflight mode={mode} concurrency={concurrency} "
        f"latency_ms={latency_ms:.1f} count_sql={payload['sql_count']} "
        f"rss_peak_mib={int(payload['rss_peak_bytes']) / 1024 / 1024:.1f} "
        f"total={totals[0]}"
    )
    return payload


async def _main(
    repeats: int,
    target_ms: float,
    offsets: tuple[int, ...],
    *,
    sql_budget: int,
    require_scale: bool,
    count_concurrency: int,
    baseline_file: Path | None,
    require_baseline: bool,
    output: Path | None,
) -> int:
    failed = False
    measurements: dict[str, float] = {}
    scale: dict[str, int] = {}
    async with async_session() as db:
        scale = await _scale_snapshot(db)
        print(
            "phase=scale "
            + " ".join(f"{name}={value}" for name, value in scale.items())
        )
        if require_scale:
            undersized = {
                name: (scale[name], minimum)
                for name, minimum in SCALE_REQUIREMENTS.items()
                if scale[name] < minimum
            }
            if undersized:
                print(f"phase=scale status=failed undersized={undersized}")
                failed = True

        service = SearchService(db)
        for case in BENCHMARK_CASES:
            case_offsets = (0,) if case.cursor_page else offsets
            case_target_ms = (
                min(target_ms, NEW_SORT_TARGET_MS)
                if case.name in {"heat", "random"}
                else target_ms
            )
            for offset in case_offsets:
                samples = await _measure(
                    service,
                    case=case.name,
                    query=case.query,
                    offset=offset,
                    repeats=repeats,
                    seed=case.seed,
                )
                p95, max_sql = _print_samples(
                    case=case.name,
                    offset=offset,
                    samples=samples,
                    target_ms=case_target_ms,
                )
                key = f"{case.name}:first" if offset == 0 else f"{case.name}:offset-{offset}"
                measurements[key] = p95
                failed = failed or p95 > case_target_ms or max_sql > sql_budget

        for case in (item for item in BENCHMARK_CASES if item.cursor_page):
            first = await service.search(
                case.query,
                0,
                30,
                scope="works",
                permissions={"library", "curation"},
                seed=case.seed,
            )
            cursor = first.get("next_cursor")
            if not cursor:
                print(f"phase=work_list case={case.name}_cursor_next status=missing_cursor")
                failed = failed or require_scale
                continue
            case_target_ms = (
                min(target_ms, NEW_SORT_TARGET_MS)
                if case.name in {"heat", "random"}
                else target_ms
            )
            samples = [
                await _sample_search(
                    service,
                    case=f"{case.name}_cursor_next",
                    query=case.query,
                    offset=0,
                    cursor=cursor,
                    seed=case.seed,
                )
                for _ in range(repeats)
            ]
            p95, max_sql = _print_samples(
                case=f"{case.name}_cursor_next",
                offset="seek",
                samples=samples,
                target_ms=case_target_ms,
            )
            measurements[f"{case.name}:next"] = p95
            failed = failed or p95 > case_target_ms or max_sql > sql_budget

    # Use fresh sessions for the concurrent round.  A single AsyncSession is
    # intentionally never shared by concurrent tasks.  This validates the SQL
    # invariant and in-process fan-in plus the Redis lease code path; it is not
    # a substitute for a later multi-process/HTTP load run on the deployed
    # backend replicas.
    cold = await _count_dogpile_round(
        mode="cold",
        concurrency=count_concurrency,
        delete_stale=True,
    )
    stale = await _count_dogpile_round(
        mode="stale",
        concurrency=count_concurrency,
        delete_stale=False,
    )
    failed = failed or int(cold["sql_count"]) != 1 or int(stale["sql_count"]) != 1

    baseline: dict[str, float] = {}
    if baseline_file and baseline_file.is_file():
        payload = json.loads(baseline_file.read_text())
        raw_baseline = payload.get("measurements", payload)
        baseline = {str(key): float(value) for key, value in raw_baseline.items()}
    elif require_baseline:
        print(f"phase=existing_sort_regression status=failed missing_baseline={baseline_file}")
        failed = True

    if require_baseline:
        required = {f"{name}:first" for name in EXISTING_SORT_CASES}
        missing = sorted(required - baseline.keys())
        if missing:
            print(f"phase=existing_sort_regression status=failed missing_cases={missing}")
            failed = True
    regressions = existing_sort_regressions(measurements, baseline)
    if regressions:
        print(f"phase=existing_sort_regression status=failed regressions={regressions}")
        failed = True

    report = {
        "scale": scale,
        "measurements": measurements,
        "targets": {
            "heat_random_p95_ms": NEW_SORT_TARGET_MS,
            "existing_sort_max_regression": MAX_EXISTING_SORT_REGRESSION,
            "sql_budget": sql_budget,
        },
        "existing_sort_regressions": regressions,
    }
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--target-ms", type=float, default=500.0)
    parser.add_argument("--sql-budget", type=int, default=WORK_SEARCH_SQL_BUDGET)
    parser.add_argument("--count-concurrency", type=int, default=20)
    parser.add_argument("--baseline-file", type=Path)
    parser.add_argument(
        "--require-baseline",
        action="store_true",
        help="Fail unless a complete pre-release existing-sort baseline is provided",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--require-scale",
        action="store_true",
        help="Fail unless the production-scale works/assets/tags/artifacts floor is present",
    )
    parser.add_argument(
        "--offsets",
        default="0,30,60000",
        help="Comma-separated page offsets; include a deep page for regression coverage",
    )
    args = parser.parse_args()
    offsets = tuple(sorted({max(0, int(value)) for value in args.offsets.split(",")}))
    raise SystemExit(asyncio.run(_main(
        max(1, args.repeats),
        args.target_ms,
        offsets,
        sql_budget=max(1, args.sql_budget),
        require_scale=bool(args.require_scale),
        count_concurrency=max(2, args.count_concurrency),
        baseline_file=args.baseline_file,
        require_baseline=bool(args.require_baseline),
        output=args.output,
    )))
