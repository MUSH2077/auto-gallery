from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterable, Mapping
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.models.source_ranking_snapshot import SourceRankingSnapshot
from app.models.work import Work
from app.models.work_source import WorkSource


OFFICIAL_RANK_MAX_AGE = timedelta(hours=48)
BAYESIAN_PRIOR_VIEWS = 20


@dataclass(frozen=True)
class SourceMetrics:
    primary_count: int | None
    view_count: int | None
    observed_at: datetime


@dataclass(frozen=True)
class HeatCandidate:
    work_source_id: UUID
    work_id: UUID
    posted_at: datetime | None
    primary_count: int | None
    view_count: int | None = None
    official_rank: int | None = None
    official_total: int | None = None
    official_fetched_at: datetime | None = None


_METRIC_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "pixiv": (
        ("total_bookmarks", "bookmarks", "bookmark_count"),
        ("total_view", "total_views", "view_count", "views"),
    ),
    "x": (
        ("favorite_count", "like_count", "favorites", "likes"),
        ("view_count", "views", "impression_count", "impressions"),
    ),
    "weibo": (
        ("attitudes_count", "like_count", "likes"),
        ("reads_count", "view_count", "views"),
    ),
    "iwara": (
        ("like_count", "likes", "favorite_count", "favorites"),
        ("view_count", "views"),
    ),
    "danbooru": (
        ("fav_count", "favorite_count", "score"),
        ("view_count", "views"),
    ),
    "pinterest": (
        ("save_count", "repin_count", "saves"),
        ("view_count", "views"),
    ),
    "lofter": (
        ("like_count", "likes", "notes"),
        ("view_count", "views"),
    ),
    "bilibili": (
        ("like", "like_count", "favorite", "favorite_count"),
        ("view", "view_count", "views"),
    ),
}


def _nonnegative_count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _first_count(metadata: Mapping[str, object], fields: tuple[str, ...]) -> int | None:
    for field in fields:
        value = _nonnegative_count(metadata.get(field))
        if value is not None:
            return value
    return None


def extract_source_metrics(
    source: str,
    metadata: Mapping[str, object] | None,
    observed_at: datetime,
) -> SourceMetrics:
    fields = _METRIC_FIELDS.get(source.lower())
    if fields is None or not isinstance(metadata, Mapping):
        return SourceMetrics(None, None, observed_at)
    primary_fields, view_fields = fields
    return SourceMetrics(
        primary_count=_first_count(metadata, primary_fields),
        view_count=_first_count(metadata, view_fields),
        observed_at=observed_at,
    )


def stable_shuffle_key(work_id: UUID) -> int:
    digest = hashlib.md5(str(work_id).encode("ascii"), usedforsecurity=False).digest()[:8]
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


def age_bucket(posted_at: datetime | None, observed_at: datetime) -> int:
    if posted_at is None:
        return 3
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=UTC)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    age = max(timedelta(), observed_at - posted_at)
    if age < timedelta(days=3):
        return 0
    if age < timedelta(days=14):
        return 1
    if age < timedelta(days=90):
        return 2
    return 3


def _fresh_official(candidate: HeatCandidate, now: datetime) -> bool:
    fetched_at = candidate.official_fetched_at
    if (
        candidate.official_rank is None
        or candidate.official_total is None
        or candidate.official_rank < 1
        or candidate.official_total < candidate.official_rank
        or fetched_at is None
    ):
        return False
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    return timedelta() <= now - fetched_at <= OFFICIAL_RANK_MAX_AGE


def _expanded_cohort(
    candidate: HeatCandidate,
    fallback: list[HeatCandidate],
    *,
    observed_at: datetime,
    minimum: int,
) -> list[HeatCandidate]:
    target = age_bucket(candidate.posted_at, observed_at)
    included = {target}
    result = [item for item in fallback if age_bucket(item.posted_at, observed_at) in included]
    distance = 1
    while len(result) < minimum and len(included) < 4:
        for bucket in (target - distance, target + distance):
            if 0 <= bucket <= 3:
                included.add(bucket)
        result = [item for item in fallback if age_bucket(item.posted_at, observed_at) in included]
        distance += 1
    return result


def _primary_percentile(candidate: HeatCandidate, cohort: list[HeatCandidate]) -> float:
    assert candidate.primary_count is not None
    counts = [item.primary_count for item in cohort if item.primary_count is not None]
    if not counts:
        return 0.0
    return sum(value <= candidate.primary_count for value in counts) / len(counts)


def _source_baseline(candidates: list[HeatCandidate]) -> float:
    primary = 0
    views = 0
    for item in candidates:
        if item.primary_count is None or item.view_count is None or item.view_count <= 0:
            continue
        primary += min(item.primary_count, item.view_count)
        views += item.view_count
    return primary / views if views else 0.0


def _smoothed_rate(candidate: HeatCandidate, baseline: float) -> float:
    if candidate.primary_count is None or candidate.view_count is None or candidate.view_count <= 0:
        return baseline
    successes = min(candidate.primary_count, candidate.view_count)
    return (successes + BAYESIAN_PRIOR_VIEWS * baseline) / (
        candidate.view_count + BAYESIAN_PRIOR_VIEWS
    )


def score_source_candidates(
    candidates: Iterable[HeatCandidate],
    *,
    now: datetime,
    min_cohort_size: int = 30,
) -> dict[UUID, float | None]:
    items = list(candidates)
    minimum = max(1, min_cohort_size)
    fallback = [item for item in items if item.primary_count is not None]
    baseline = _source_baseline(fallback)

    sortable: list[tuple[tuple[object, ...], HeatCandidate]] = []
    result: dict[UUID, float | None] = {
        item.work_source_id: None for item in items
    }
    for item in items:
        if _fresh_official(item, now):
            official_position = item.official_rank / item.official_total  # type: ignore[operator]
            key = (0, official_position, item.work_source_id.int)
        elif item.primary_count is not None:
            cohort = _expanded_cohort(
                item,
                fallback,
                observed_at=now,
                minimum=minimum,
            )
            percentile = _primary_percentile(item, cohort)
            rate = _smoothed_rate(item, baseline)
            posted = item.posted_at.timestamp() if item.posted_at is not None else 0.0
            key = (1, -percentile, -rate, -posted, item.work_source_id.int)
        else:
            continue
        sortable.append((key, item))

    sortable.sort(key=lambda entry: entry[0])
    count = len(sortable)
    for position, (_key, item) in enumerate(sortable):
        result[item.work_source_id] = (
            100.0 if count == 1 else 100.0 * (count - position - 1) / (count - 1)
        )
    return result


def fallback_heat_rows(
    candidates: Iterable[HeatCandidate],
    *,
    now: datetime,
    min_cohort_size: int = 30,
) -> dict[UUID, float | None]:
    """Public pure scoring interface used by rebuilds and focused backfills."""

    return score_source_candidates(
        candidates,
        now=now,
        min_cohort_size=min_cohort_size,
    )


def aggregate_work_heat(
    candidates: Iterable[HeatCandidate],
    source_scores: Mapping[UUID, float | None],
) -> dict[UUID, float | None]:
    result: dict[UUID, float | None] = {}
    for item in candidates:
        score = source_scores.get(item.work_source_id)
        current = result.get(item.work_id)
        if score is not None and (current is None or score > current):
            result[item.work_id] = score
        elif item.work_id not in result:
            result[item.work_id] = None
    return result


async def recompute_source_heat(
    db,
    sources: Iterable[str],
    *,
    now: datetime | None = None,
    request_projection=None,
) -> set[UUID]:
    observed_at = now or datetime.now(UTC)
    normalized_sources = {source.strip().lower() for source in sources if source.strip()}
    if not normalized_sources:
        return set()

    source_rows = (
        await db.execute(
            select(WorkSource)
            .options(selectinload(WorkSource.work))
            .where(WorkSource.source.in_(normalized_sources))
        )
    ).scalars().unique().all()
    if not source_rows:
        return set()

    ranking_cutoff = observed_at - OFFICIAL_RANK_MAX_AGE
    latest_ranking_date = {
        source: ranking_date
        for source, ranking_date in (
            await db.execute(
                select(
                    SourceRankingSnapshot.source,
                    func.max(SourceRankingSnapshot.ranking_date),
                )
                .where(
                    SourceRankingSnapshot.source.in_(normalized_sources),
                    SourceRankingSnapshot.fetched_at >= ranking_cutoff,
                )
                .group_by(SourceRankingSnapshot.source)
            )
        ).all()
        if ranking_date is not None
    }
    source_work_ids = {row.source_work_id for row in source_rows}
    ranking_rows = (
        await db.execute(
            select(SourceRankingSnapshot).where(
                SourceRankingSnapshot.source.in_(normalized_sources),
                SourceRankingSnapshot.source_work_id.in_(source_work_ids),
                SourceRankingSnapshot.fetched_at >= ranking_cutoff,
            )
        )
    ).scalars().all()
    best_rank: dict[tuple[str, str], SourceRankingSnapshot] = {}
    for ranking in ranking_rows:
        if ranking.ranking_date != latest_ranking_date.get(ranking.source):
            continue
        key = (ranking.source, ranking.source_work_id)
        current = best_rank.get(key)
        if current is None or ranking.rank / ranking.rank_total < current.rank / current.rank_total:
            best_rank[key] = ranking

    candidates_by_source: dict[str, list[HeatCandidate]] = {}
    row_by_id: dict[UUID, WorkSource] = {}
    for row in source_rows:
        metric_time = row.metrics_observed_at or row.updated_at or observed_at
        metrics = extract_source_metrics(row.source, row.raw_metadata, metric_time)
        if (
            metrics.primary_count != row.engagement_count
            or metrics.view_count != row.view_count
        ):
            metrics = extract_source_metrics(
                row.source,
                row.raw_metadata,
                row.updated_at or observed_at,
            )
        row.engagement_count = metrics.primary_count
        row.view_count = metrics.view_count
        row.metrics_observed_at = metrics.observed_at
        ranking = best_rank.get((row.source, row.source_work_id))
        candidate = HeatCandidate(
            work_source_id=row.id,
            work_id=row.work_id,
            posted_at=row.posted_at or (row.work.posted_at if row.work is not None else None),
            primary_count=metrics.primary_count,
            view_count=metrics.view_count,
            official_rank=ranking.rank if ranking is not None else None,
            official_total=ranking.rank_total if ranking is not None else None,
            official_fetched_at=ranking.fetched_at if ranking is not None else None,
        )
        candidates_by_source.setdefault(row.source, []).append(candidate)
        row_by_id[row.id] = row

    affected_work_ids: set[UUID] = set()
    for candidates in candidates_by_source.values():
        scores = score_source_candidates(candidates, now=observed_at)
        for candidate in candidates:
            row = row_by_id[candidate.work_source_id]
            score = scores[candidate.work_source_id]
            row.source_heat_score = score
            row.heat_basis = (
                "official_rank"
                if _fresh_official(candidate, observed_at)
                else "local_fallback"
                if score is not None
                else None
            )
            affected_work_ids.add(candidate.work_id)

    works = (
        await db.execute(
            select(Work)
            .options(selectinload(Work.work_sources))
            .where(Work.id.in_(affected_work_ids))
        )
    ).scalars().unique().all()
    changed_work_ids: set[UUID] = set()
    for work in works:
        available = [
            source.source_heat_score
            for source in work.work_sources
            if source.source_heat_score is not None
        ]
        next_score = max(available) if available else None
        if work.heat_score != next_score:
            work.heat_score = next_score
            work.heat_observed_at = observed_at
            changed_work_ids.add(work.id)

    await db.flush()
    if changed_work_ids:
        if request_projection is None:
            from app.services.search_projection_outbox import request_search_projection

            request_projection = request_search_projection
        await request_projection(db, sorted(changed_work_ids, key=str))
    return changed_work_ids
