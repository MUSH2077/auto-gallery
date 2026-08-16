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


QUERIES = (
    ("default", ""),
    ("posted", "sort:posted-desc"),
    ("updated", "sort:updated-desc"),
    ("source", "source:pixiv"),
    ("favorite", "is:favorite"),
    ("title", "is:sfw sort:title-asc"),
    ("multi_asset", "has:multiple-assets"),
)

SCALE_REQUIREMENTS = {
    "works": 67_000,
    "assets": 90_000,
    "tag_relations": 470_000,
    "artifacts": 330_000,
}


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
) -> list[dict[str, Any]]:
    # Warm query plans, relation pages, and the exact-count cache outside the
    # measured window.
    await service.search(
        query,
        offset,
        30,
        scope="works",
        permissions={"library", "curation"},
    )
    return [
        await _sample_search(
            service,
            case=case,
            query=query,
            offset=offset,
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
) -> int:
    failed = False
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
        for name, query in QUERIES:
            for offset in offsets:
                samples = await _measure(
                    service,
                    case=name,
                    query=query,
                    offset=offset,
                    repeats=repeats,
                )
                p95, max_sql = _print_samples(
                    case=name,
                    offset=offset,
                    samples=samples,
                    target_ms=target_ms,
                )
                failed = failed or p95 >= target_ms or max_sql > sql_budget

        first = await service.search(
            "",
            0,
            30,
            scope="works",
            permissions={"library", "curation"},
        )
        cursor = first.get("next_cursor")
        if cursor:
            samples = [
                await _sample_search(
                    service,
                    case="cursor_next",
                    query="",
                    offset=0,
                    cursor=cursor,
                )
                for _ in range(repeats)
            ]
            p95, max_sql = _print_samples(
                case="cursor_next",
                offset="seek",
                samples=samples,
                target_ms=target_ms,
            )
            failed = failed or p95 >= target_ms or max_sql > sql_budget

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
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--target-ms", type=float, default=500.0)
    parser.add_argument("--sql-budget", type=int, default=4)
    parser.add_argument("--count-concurrency", type=int, default=20)
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
    )))
