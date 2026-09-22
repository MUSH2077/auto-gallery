from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
import os
import subprocess
import sys
from time import perf_counter
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.models.work import Work
from app.models.work_source import WorkSource
from app.models.source_ranking_snapshot import SourceRankingSnapshot

from app.services.work_heat import (
    HeatCandidate,
    age_bucket,
    aggregate_work_heat,
    extract_source_metrics,
    fallback_heat_rows,
    official_rank_expired,
    recompute_source_heat,
    score_source_candidates,
    stable_shuffle_key,
)


NOW = datetime(2026, 9, 22, 4, 0, tzinfo=UTC)


def candidate(
    number: int,
    *,
    age_days: float,
    primary: int | None,
    views: int | None = None,
    rank: int | None = None,
    rank_total: int | None = None,
    rank_age_hours: int = 1,
    work_number: int | None = None,
) -> HeatCandidate:
    return HeatCandidate(
        work_source_id=UUID(int=number),
        work_id=UUID(int=work_number or number),
        posted_at=NOW - timedelta(days=age_days),
        primary_count=primary,
        view_count=views,
        official_rank=rank,
        official_total=rank_total,
        official_fetched_at=(NOW - timedelta(hours=rank_age_hours)) if rank is not None else None,
    )


def test_stable_shuffle_key_is_repeatable_and_bounded():
    work_id = UUID("01234567-89ab-cdef-0123-456789abcdef")

    first = stable_shuffle_key(work_id)

    assert first == stable_shuffle_key(work_id)
    assert first != stable_shuffle_key(UUID(int=work_id.int + 1))
    assert 0 <= first <= 2**63 - 1


def test_extract_source_metrics_uses_explicit_provider_fields():
    pixiv = extract_source_metrics(
        "pixiv",
        {"total_bookmarks": 34, "total_view": 500, "unrelated": 9999},
        NOW,
    )
    x = extract_source_metrics(
        "x",
        {"favorite_count": 12, "retweet_count": 50, "view_count": 400},
        NOW,
    )

    assert (pixiv.primary_count, pixiv.view_count, pixiv.observed_at) == (34, 500, NOW)
    assert (x.primary_count, x.view_count) == (12, 400)
    assert extract_source_metrics("manual", {"likes": 100}, NOW).primary_count is None


def test_age_bucket_has_four_stable_boundaries():
    assert age_bucket(NOW - timedelta(days=2, hours=23), NOW) == 0
    assert age_bucket(NOW - timedelta(days=3), NOW) == 1
    assert age_bucket(NOW - timedelta(days=14), NOW) == 2
    assert age_bucket(NOW - timedelta(days=90), NOW) == 3


def test_fresh_official_rank_precedes_fallback_but_stale_rank_does_not():
    fresh = candidate(1, age_days=1, primary=0, rank=10, rank_total=500)
    popular_fallback = candidate(2, age_days=1, primary=500, views=1000)
    stale = candidate(3, age_days=1, primary=1, rank=1, rank_total=500, rank_age_hours=49)

    scores = score_source_candidates([fresh, popular_fallback, stale], now=NOW, min_cohort_size=1)

    assert scores[fresh.work_source_id] > scores[popular_fallback.work_source_id]
    assert scores[popular_fallback.work_source_id] > scores[stale.work_source_id]


def test_official_rank_expiry_has_an_explicit_48_hour_boundary():
    fetched_at = NOW - timedelta(hours=48)

    assert official_rank_expired(fetched_at, NOW - timedelta(minutes=1)) is False
    assert official_rank_expired(fetched_at, NOW + timedelta(minutes=1)) is True


def test_fallback_compares_primary_counts_inside_age_cohort():
    new_work = candidate(1, age_days=1, primary=8, views=100)
    old_work = candidate(2, age_days=365, primary=500, views=10000)
    new_peer = candidate(3, age_days=1, primary=2, views=100)
    old_peer = candidate(4, age_days=365, primary=1000, views=10000)

    scores = score_source_candidates(
        [new_work, old_work, new_peer, old_peer],
        now=NOW,
        min_cohort_size=2,
    )

    assert scores[new_work.work_source_id] > scores[new_peer.work_source_id]
    assert scores[old_peer.work_source_id] > scores[old_work.work_source_id]
    assert fallback_heat_rows(
        [new_work, old_work, new_peer, old_peer],
        now=NOW,
        min_cohort_size=2,
    ) == scores


def test_small_cohort_widens_before_percentile_is_calculated():
    isolated_new = candidate(1, age_days=1, primary=20, views=100)
    older = [
        candidate(number, age_days=5, primary=primary, views=100)
        for number, primary in ((2, 10), (3, 30), (4, 40))
    ]

    scores = score_source_candidates(
        [isolated_new, *older],
        now=NOW,
        min_cohort_size=3,
    )

    assert scores[older[1].work_source_id] > scores[isolated_new.work_source_id]
    assert scores[isolated_new.work_source_id] > scores[older[0].work_source_id]


def test_bayesian_rate_breaks_equal_primary_count_ties_without_promoting_one_of_one():
    efficient = candidate(1, age_days=5, primary=10, views=100)
    inefficient = candidate(2, age_days=5, primary=10, views=1000)
    tiny_sample = candidate(3, age_days=5, primary=1, views=1)

    scores = score_source_candidates(
        [efficient, inefficient, tiny_sample],
        now=NOW,
        min_cohort_size=1,
    )

    assert scores[efficient.work_source_id] > scores[inefficient.work_source_id]
    assert scores[efficient.work_source_id] > scores[tiny_sample.work_source_id]


def test_source_scoring_handles_70000_candidates_without_quadratic_regression():
    candidates = [
        candidate(
            number,
            age_days=number % 120,
            primary=number % 1000,
            views=1000 + number % 1000,
        )
        for number in range(1, 70_001)
    ]

    started = perf_counter()
    scores = score_source_candidates(candidates, now=NOW)
    elapsed = perf_counter() - started

    assert len(scores) == 70_000
    assert elapsed < 5.0


def test_missing_metrics_remain_null_and_multi_source_uses_best_score():
    missing = candidate(1, age_days=10, primary=None, views=None)
    first_source = candidate(2, age_days=10, primary=4, work_number=20)
    second_source = candidate(3, age_days=10, primary=8, work_number=20)
    scores = score_source_candidates(
        [missing, first_source, second_source],
        now=NOW,
        min_cohort_size=1,
    )

    aggregated = aggregate_work_heat([missing, first_source, second_source], scores)

    assert scores[missing.work_source_id] is None
    assert aggregated[missing.work_id] is None
    assert aggregated[first_source.work_id] == max(
        scores[first_source.work_source_id], scores[second_source.work_source_id]
    )


class ScalarRows:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def unique(self):
        return self

    def all(self):
        return self.values


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)

    async def execute(self, _statement):
        return ScalarRows(self.responses.pop(0))

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_recompute_source_heat_materializes_changed_works_and_requests_projection():
    first_work = Work(id=UUID(int=101), title="first", posted_at=NOW - timedelta(days=1))
    second_work = Work(id=UUID(int=102), title="second", posted_at=NOW - timedelta(days=1))
    first_source = WorkSource(
        id=UUID(int=201),
        work_id=first_work.id,
        source="pixiv",
        source_work_id="p1",
        raw_metadata={"bookmarks": 20, "views": 200},
        engagement_count=20,
        view_count=200,
        metrics_observed_at=NOW - timedelta(days=2),
        work=first_work,
    )
    first_source.updated_at = NOW - timedelta(hours=1)
    second_source = WorkSource(
        id=UUID(int=202),
        work_id=second_work.id,
        source="pixiv",
        source_work_id="p2",
        raw_metadata={"bookmarks": 5, "views": 200},
        work=second_work,
    )
    first_work.work_sources = [first_source]
    second_work.work_sources = [second_source]
    db = FakeSession([[first_source, second_source], [], [], [first_work, second_work]])
    projected: list[UUID] = []

    async def request_projection(_db, work_ids):
        projected.extend(work_ids)

    result = await recompute_source_heat(
        db,  # type: ignore[arg-type]
        {"pixiv"},
        now=NOW,
        request_projection=request_projection,
    )

    assert result == {first_work.id, second_work.id}
    assert first_source.engagement_count == 20
    assert first_source.view_count == 200
    assert first_source.metrics_observed_at == NOW - timedelta(days=2)
    assert first_source.source_heat_score > second_source.source_heat_score
    assert first_work.heat_score == first_source.source_heat_score
    assert second_work.heat_score == second_source.source_heat_score
    assert projected == [first_work.id, second_work.id]

    repeat_db = FakeSession([[first_source, second_source], [], [], [first_work, second_work]])
    repeated = await recompute_source_heat(
        repeat_db,  # type: ignore[arg-type]
        {"pixiv"},
        now=NOW,
        request_projection=request_projection,
    )
    assert repeated == set()
    assert projected == [first_work.id, second_work.id]


@pytest.mark.asyncio
async def test_recompute_uses_only_the_latest_complete_daily_ranking_batch():
    first_work = Work(id=UUID(int=111), title="first", posted_at=NOW - timedelta(days=1))
    second_work = Work(id=UUID(int=112), title="second", posted_at=NOW - timedelta(days=1))
    first_source = WorkSource(
        id=UUID(int=211),
        work_id=first_work.id,
        source="pixiv",
        source_work_id="p1",
        raw_metadata={"bookmarks": 1, "views": 100},
        work=first_work,
    )
    second_source = WorkSource(
        id=UUID(int=212),
        work_id=second_work.id,
        source="pixiv",
        source_work_id="p2",
        raw_metadata={"bookmarks": 1, "views": 100},
        work=second_work,
    )
    first_work.work_sources = [first_source]
    second_work.work_sources = [second_source]
    rankings = [
        SourceRankingSnapshot(
            source="pixiv",
            mode="day",
            ranking_date=date(2026, 9, 20),
            source_work_id="p1",
            rank=1,
            rank_total=100,
            fetched_at=NOW - timedelta(hours=2),
        ),
        SourceRankingSnapshot(
            source="pixiv",
            mode="day",
            ranking_date=date(2026, 9, 21),
            source_work_id="p1",
            rank=100,
            rank_total=100,
            fetched_at=NOW - timedelta(hours=1),
        ),
        SourceRankingSnapshot(
            source="pixiv",
            mode="day",
            ranking_date=date(2026, 9, 21),
            source_work_id="p2",
            rank=1,
            rank_total=100,
            fetched_at=NOW - timedelta(hours=1),
        ),
    ]
    db = FakeSession([
        [first_source, second_source],
        [("pixiv", date(2026, 9, 21))],
        rankings,
        [first_work, second_work],
    ])

    await recompute_source_heat(
        db,  # type: ignore[arg-type]
        {"pixiv"},
        now=NOW,
        request_projection=lambda *_args: asyncio.sleep(0),
    )

    assert second_source.source_heat_score > first_source.source_heat_score
    assert first_source.heat_basis == "official_rank"
    assert second_source.heat_basis == "official_rank"


@pytest.mark.asyncio
async def test_recompute_drops_an_old_rank_when_no_local_work_is_in_latest_batch():
    work = Work(id=UUID(int=121), title="dropped", posted_at=NOW - timedelta(days=1))
    source = WorkSource(
        id=UUID(int=221),
        work_id=work.id,
        source="pixiv",
        source_work_id="dropped-from-today",
        raw_metadata={"bookmarks": 4, "views": 100},
        work=work,
    )
    work.work_sources = [source]
    old_ranking = SourceRankingSnapshot(
        source="pixiv",
        mode="day",
        ranking_date=date(2026, 9, 20),
        source_work_id=source.source_work_id,
        rank=1,
        rank_total=100,
        fetched_at=NOW - timedelta(hours=2),
    )
    db = FakeSession([
        [source],
        [("pixiv", date(2026, 9, 21))],
        [old_ranking],
        [work],
    ])

    await recompute_source_heat(
        db,  # type: ignore[arg-type]
        {"pixiv"},
        now=NOW,
        request_projection=lambda *_args: asyncio.sleep(0),
    )

    assert source.heat_basis == "local_fallback"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_latest_ranking_date_is_resolved_before_filtering_to_local_works():
    from sqlalchemy import delete, select

    from app.database import async_session

    source_name = f"heat-boundary-{uuid4().hex[:12]}"
    local_source_work_id = f"local-{uuid4().hex}"
    external_source_work_id = f"external-{uuid4().hex}"
    work_id = uuid4()
    source_id = uuid4()

    async def ignore_projection(_db, _work_ids):
        return None

    try:
        async with async_session() as db:
            work = Work(id=work_id, title="ranking boundary", posted_at=NOW - timedelta(days=1))
            source = WorkSource(
                id=source_id,
                work_id=work_id,
                source=source_name,
                source_work_id=local_source_work_id,
                raw_metadata={},
            )
            db.add_all([
                work,
                source,
                SourceRankingSnapshot(
                    source=source_name,
                    mode="day",
                    ranking_date=date(2026, 9, 20),
                    source_work_id=local_source_work_id,
                    rank=1,
                    rank_total=100,
                    fetched_at=NOW - timedelta(hours=2),
                ),
                SourceRankingSnapshot(
                    source=source_name,
                    mode="day",
                    ranking_date=date(2026, 9, 21),
                    source_work_id=external_source_work_id,
                    rank=1,
                    rank_total=100,
                    fetched_at=NOW - timedelta(hours=1),
                ),
            ])
            await db.commit()

        async with async_session() as db:
            await recompute_source_heat(
                db,
                {source_name},
                now=NOW,
                request_projection=ignore_projection,
            )
            await db.commit()
            stored = (
                await db.execute(select(WorkSource).where(WorkSource.id == source_id))
            ).scalar_one()
            assert stored.heat_basis is None
            assert stored.source_heat_score is None
    finally:
        async with async_session() as db:
            await db.execute(
                delete(SourceRankingSnapshot).where(SourceRankingSnapshot.source == source_name)
            )
            await db.execute(delete(WorkSource).where(WorkSource.id == source_id))
            await db.execute(delete(Work).where(Work.id == work_id))
            await db.commit()


def test_bulk_import_rows_include_stable_shuffle_and_source_metric_snapshot(
    tmp_path,
    monkeypatch,
):
    from app.config import settings
    from app.jobs.import_runner import _build_import_work

    monkeypatch.setattr(settings, "download_root", str(tmp_path))
    media_path = tmp_path / "101_p0.jpg"
    media_path.write_bytes(b"fixture")
    raw_metadata = {
        "id": 101,
        "total_bookmarks": 42,
        "total_view": 700,
        "x_restrict": 0,
        "illust_ai_type": 1,
    }
    media_work = {
        "source_work_id": "101",
        "prepared_work": {
            "first_raw": raw_metadata,
            "ws_data": {
                "source": "pixiv",
                "source_url": "https://www.pixiv.net/artworks/101",
                "source_creator_id": "9",
                "title": "fixture",
                "description": None,
                "raw_metadata": raw_metadata,
            },
            "posted_at": NOW,
            "tags": [],
            "linked_creator_id": None,
            "creator_archived": False,
        },
        "asset_files": [media_path],
        "file_stats": {media_path: media_path.stat()},
        "primary_values": {},
        "creator_dir": "9",
        "lib_dir": tmp_path,
        "display_name": "Artist",
        "work_dir": tmp_path,
    }

    staged = _build_import_work(
        provider=SimpleNamespace(source_name="pixiv"),
        download_job=SimpleNamespace(subscription_source_id=None, subscription_id=None),
        media_work=media_work,
    )

    work_row = staged["rows"]["works"][0]
    source_row = staged["rows"]["work_sources"][0]
    assert "shuffle_key" not in work_row
    assert 0 <= stable_shuffle_key(work_row["id"]) <= 2**63 - 1
    assert source_row["engagement_count"] == 42
    assert source_row["view_count"] == 700
    assert source_row["metrics_observed_at"].tzinfo is UTC


def test_heat_recompute_request_coalesces_queued_work_and_follows_running_work(
    monkeypatch,
):
    from rq.exceptions import NoSuchJobError

    from app.services import work_heat_queue

    class ExistingJob:
        def __init__(self, status, *, deletable=False):
            self.status = status
            self.deletable = deletable
            self.deleted = False

        def get_status(self, refresh=True):
            assert refresh is True
            return self.status

        def delete(self):
            if not self.deletable:
                raise AssertionError("active jobs must not be deleted")
            self.deleted = True

    queued_calls = []
    statuses = {"work-heat-pixiv": ExistingJob("queued")}

    def fetch(job_id, *, connection):
        assert connection is redis
        try:
            return statuses[job_id]
        except KeyError as exc:
            raise NoSuchJobError from exc

    def enqueue(queue, function, source, **kwargs):
        queued_calls.append((queue, function, source, kwargs))

    redis = object()
    queue = object()
    monkeypatch.setattr(work_heat_queue.Job, "fetch", fetch)
    monkeypatch.setattr(work_heat_queue, "Queue", lambda **_kwargs: queue)
    monkeypatch.setattr(work_heat_queue, "checked_enqueue", enqueue)

    queued = work_heat_queue.request_work_heat_recompute(
        {"pixiv"}, redis_client=redis
    )
    assert queued == {"created": 0, "coalesced": 1, "errors": 0}
    assert queued_calls == []

    statuses["work-heat-pixiv"] = ExistingJob("started")
    running = work_heat_queue.request_work_heat_recompute(
        {"pixiv"}, redis_client=redis
    )
    assert running == {"created": 1, "coalesced": 0, "errors": 0}
    assert queued_calls[0][2] == "pixiv"
    assert queued_calls[0][3]["job_id"] == "work-heat-pixiv-followup"
    assert queued_calls[0][3]["depends_on"].dependencies == ["work-heat-pixiv"]
    assert queued_calls[0][3]["depends_on"].allow_failure is True

    queued_calls.clear()
    completed_primary = ExistingJob("finished", deletable=True)
    statuses["work-heat-pixiv"] = completed_primary
    statuses["work-heat-pixiv-followup"] = ExistingJob("started")
    followup_running = work_heat_queue.request_work_heat_recompute(
        {"pixiv"}, redis_client=redis
    )
    assert followup_running == {"created": 1, "coalesced": 0, "errors": 0}
    assert completed_primary.deleted is True
    assert queued_calls[0][3]["job_id"] == "work-heat-pixiv"
    assert queued_calls[0][3]["depends_on"].dependencies == [
        "work-heat-pixiv-followup"
    ]
    assert queued_calls[0][3]["depends_on"].allow_failure is True

    queued_calls.clear()
    statuses["work-heat-pixiv"] = ExistingJob("deferred")
    successor_pending = work_heat_queue.request_work_heat_recompute(
        {"pixiv"}, redis_client=redis
    )
    assert successor_pending == {"created": 0, "coalesced": 1, "errors": 0}
    assert queued_calls == []


def test_heat_recompute_enqueue_lock_prevents_duplicate_job_ids(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock

    from rq import Queue
    from rq.exceptions import NoSuchJobError
    from rq.job import Job

    from app.services import work_heat_queue
    from app.services.redis_client import get_redis

    redis = get_redis()
    try:
        redis.ping()
    except Exception as exc:
        pytest.skip(f"Redis unavailable: {exc}")

    source = f"race-{uuid4().hex[:12]}"
    primary_id = f"work-heat-{source}"
    followup_id = f"{primary_id}-followup"
    queue = Queue("maintenance", connection=redis)
    original_enqueue = work_heat_queue.checked_enqueue
    first_entered = Event()
    release_first = Event()
    second_entered = Event()
    call_lock = Lock()
    enqueue_calls = 0

    monkeypatch.setattr(work_heat_queue, "_ENQUEUE_LOCK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(
        work_heat_queue,
        "_ENQUEUE_LOCK_RENEW_INTERVAL_SECONDS",
        0.05,
    )

    def delayed_enqueue(*args, **kwargs):
        nonlocal enqueue_calls
        with call_lock:
            enqueue_calls += 1
            call_number = enqueue_calls
        if call_number == 1:
            first_entered.set()
            assert release_first.wait(timeout=5)
        else:
            second_entered.set()
        return original_enqueue(*args, **kwargs)

    monkeypatch.setattr(work_heat_queue, "checked_enqueue", delayed_enqueue)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                work_heat_queue.request_work_heat_recompute,
                {source},
                redis_client=redis,
            )
            assert first_entered.wait(timeout=5)
            second = executor.submit(
                work_heat_queue.request_work_heat_recompute,
                {source},
                redis_client=redis,
            )
            # Keep the first producer inside enqueue for more than twice the
            # base lock TTL. Renewal must still prevent the second producer
            # from observing the uncommitted slot and writing the same job id.
            raced_past_lock = second_entered.wait(timeout=0.5)
            release_first.set()
            outcomes = (first.result(timeout=10), second.result(timeout=10))

        assert raced_past_lock is False
        assert enqueue_calls == 1
        assert sorted(outcome["created"] for outcome in outcomes) == [0, 1]
        assert queue.get_job_ids().count(primary_id) == 1
    finally:
        release_first.set()
        queue.connection.lrem(queue.key, 0, primary_id)
        queue.connection.lrem(queue.key, 0, followup_id)
        for job_id in (primary_id, followup_id):
            try:
                Job.fetch(job_id, connection=redis).delete()
            except NoSuchJobError:
                pass
        redis.delete(f"lock:work-heat:enqueue:{source}")


@pytest.mark.parametrize("terminal_status", ["failed", "stopped", "canceled"])
def test_heat_recompute_dependency_handshake_recovers_terminal_parent_race(
    monkeypatch,
    terminal_status,
):
    from rq import Queue
    from rq.exceptions import NoSuchJobError
    from rq.job import Job, JobStatus

    from app.services import work_heat_queue
    from app.services.redis_client import get_redis

    redis = get_redis()
    try:
        redis.ping()
    except Exception as exc:
        pytest.skip(f"Redis unavailable: {exc}")

    source = f"terminal-{uuid4().hex[:12]}"
    primary_id = f"work-heat-{source}"
    followup_id = f"{primary_id}-followup"
    queue = Queue("maintenance", connection=redis)
    original_enqueue = work_heat_queue.checked_enqueue
    observed_before_handshake = []

    try:
        assert work_heat_queue.request_work_heat_recompute(
            {source}, redis_client=redis
        )["created"] == 1
        primary = Job.fetch(primary_id, connection=redis)
        queue.connection.lrem(queue.key, 0, primary_id)
        primary.set_status(JobStatus.STARTED)

        def finish_parent_before_dependency_registration(*args, **kwargs):
            if kwargs.get("job_id") == followup_id and "depends_on" in kwargs:
                primary.set_status(JobStatus(terminal_status))
            job = original_enqueue(*args, **kwargs)
            if kwargs.get("job_id") == followup_id and "depends_on" in kwargs:
                observed_before_handshake.append(job.get_status(refresh=True))
            return job

        monkeypatch.setattr(
            work_heat_queue,
            "checked_enqueue",
            finish_parent_before_dependency_registration,
        )
        outcome = work_heat_queue.request_work_heat_recompute(
            {source}, redis_client=redis
        )
        followup = Job.fetch(followup_id, connection=redis)

        assert outcome == {"created": 1, "coalesced": 0, "errors": 0}
        assert observed_before_handshake == [JobStatus.DEFERRED]
        assert followup.get_status(refresh=True) == JobStatus.QUEUED
        assert queue.get_job_ids().count(followup_id) == 1
        assert not redis.sismember(Job.dependents_key_for(primary_id), followup_id)
    finally:
        queue.connection.lrem(queue.key, 0, primary_id)
        queue.connection.lrem(queue.key, 0, followup_id)
        for job_id in (primary_id, followup_id):
            try:
                Job.fetch(job_id, connection=redis).delete()
            except NoSuchJobError:
                pass
        redis.delete(f"lock:work-heat:enqueue:{source}")


@pytest.mark.parametrize("terminal_status", ["failed", "stopped", "canceled"])
def test_heat_recompute_watchdog_recovers_parent_terminal_after_handshake(
    monkeypatch,
    terminal_status,
):
    from rq import Queue
    from rq.exceptions import NoSuchJobError
    from rq.job import Job, JobStatus

    from app.services import work_heat_queue
    from app.services.redis_client import get_redis

    redis = get_redis()
    try:
        redis.ping()
    except Exception as exc:
        pytest.skip(f"Redis unavailable: {exc}")

    source = f"late-terminal-{uuid4().hex[:12]}"
    primary_id = f"work-heat-{source}"
    followup_id = f"{primary_id}-followup"
    queue = Queue("maintenance", connection=redis)

    try:
        assert work_heat_queue.request_work_heat_recompute(
            {source}, redis_client=redis
        )["created"] == 1
        primary = Job.fetch(primary_id, connection=redis)
        queue.connection.lrem(queue.key, 0, primary_id)
        primary.set_status(JobStatus.STARTED)

        assert work_heat_queue.request_work_heat_recompute(
            {source}, redis_client=redis
        )["created"] == 1
        followup = Job.fetch(followup_id, connection=redis)
        assert followup.get_status(refresh=True) == JobStatus.DEFERRED

        # The dependency was healthy during the post-enqueue handshake and
        # only became terminal afterwards. RQ does not release STOPPED or
        # CANCELED dependents, and this also models a missed FAILED promotion.
        primary.set_status(JobStatus(terminal_status))
        if terminal_status != "failed":
            queue.enqueue_dependents(primary)
            assert followup.get_status(refresh=True) == JobStatus.DEFERRED

        # This successor already passed admission when it entered Deferred.
        # Recovery must promote it in place even if admitting brand-new work is
        # currently forbidden, rather than deleting it before a fresh enqueue.
        monkeypatch.setattr(
            work_heat_queue,
            "checked_enqueue",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("deferred recovery must not re-enter admission")
            ),
        )
        outcome = work_heat_queue.reconcile_deferred_work_heat_jobs(
            redis_client=redis
        )
        followup = Job.fetch(followup_id, connection=redis)

        assert outcome["recovered"] >= 1
        assert followup.get_status(refresh=True) == JobStatus.QUEUED
        assert queue.get_job_ids().count(followup_id) == 1
    finally:
        queue.connection.lrem(queue.key, 0, primary_id)
        queue.connection.lrem(queue.key, 0, followup_id)
        for job_id in (primary_id, followup_id):
            try:
                Job.fetch(job_id, connection=redis).delete()
            except NoSuchJobError:
                pass
        redis.delete(f"lock:work-heat:enqueue:{source}")


def test_deferred_atomic_promotion_preserves_child_when_lock_is_lost_before_commit():
    from rq import Queue
    from rq.exceptions import NoSuchJobError
    from rq.job import Job, JobStatus

    from app.services import work_heat_queue
    from app.services.redis_client import get_redis

    redis = get_redis()
    try:
        redis.ping()
    except Exception as exc:
        pytest.skip(f"Redis unavailable: {exc}")

    source = f"lock-loss-{uuid4().hex[:12]}"
    primary_id = f"work-heat-{source}"
    followup_id = f"{primary_id}-followup"
    queue = Queue("maintenance", connection=redis)

    class LoseBeforeCommit:
        checks = 0

        def ensure_owned(self):
            self.checks += 1
            if self.checks == 4:
                raise RuntimeError("simulated lock loss before EXEC")

    try:
        assert work_heat_queue.request_work_heat_recompute(
            {source}, redis_client=redis
        )["created"] == 1
        primary = Job.fetch(primary_id, connection=redis)
        queue.connection.lrem(queue.key, 0, primary_id)
        primary.set_status(JobStatus.STARTED)
        assert work_heat_queue.request_work_heat_recompute(
            {source}, redis_client=redis
        )["created"] == 1
        followup = Job.fetch(followup_id, connection=redis)
        primary.set_status(JobStatus.STOPPED)

        with pytest.raises(RuntimeError, match="simulated lock loss"):
            work_heat_queue._promote_deferred_job_atomically(
                queue,
                followup,
                parent_id=primary_id,
                redis_client=redis,
                lock_guard=LoseBeforeCommit(),
            )

        followup = Job.fetch(followup_id, connection=redis)
        assert followup.get_status(refresh=True) == JobStatus.DEFERRED
        assert redis.sismember(Job.dependents_key_for(primary_id), followup_id)
        assert queue.get_job_ids().count(followup_id) == 0
    finally:
        queue.connection.lrem(queue.key, 0, primary_id)
        queue.connection.lrem(queue.key, 0, followup_id)
        for job_id in (primary_id, followup_id):
            try:
                Job.fetch(job_id, connection=redis).delete()
            except NoSuchJobError:
                pass
        redis.delete(f"lock:work-heat:enqueue:{source}")


def test_import_paths_request_heat_recompute_only_after_durable_commit():
    from inspect import getsource

    from app.jobs import import_runner

    existing_source = getsource(import_runner._update_existing_work_groups)
    live_source = getsource(import_runner.run_import_job)
    assert existing_source.index("await _commit_import(db)") < existing_source.index(
        "request_work_heat_recompute"
    )
    assert live_source.index("await _commit_import(batch_db)") < live_source.index(
        "request_work_heat_recompute"
    )


@pytest.mark.integration
def test_heat_schema_migration_backfills_shuffle_key_and_round_trips(
    test_database_url,
    test_database,
):
    assert test_database == test_database_url
    env = {**os.environ, "DATABASE_URL": test_database_url}

    def alembic(*args: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    async def prepare_and_assert() -> None:
        import asyncpg

        connection_url = test_database_url.replace(
            "postgresql+asyncpg://", "postgresql://"
        )
        connection = await asyncpg.connect(connection_url)
        try:
            await connection.execute(
                """
                INSERT INTO works (
                    id, title, is_nsfw, is_ai_generated, is_favorite
                ) VALUES (
                    '01234567-89ab-cdef-0123-456789abcdef',
                    'migration fixture', false, false, false
                )
                """
            )
        finally:
            await connection.close()

    async def assert_upgraded() -> None:
        import asyncpg

        connection_url = test_database_url.replace(
            "postgresql+asyncpg://", "postgresql://"
        )
        connection = await asyncpg.connect(connection_url)
        try:
            row = await connection.fetchrow(
                """
                SELECT shuffle_key, heat_score
                FROM works
                WHERE id = '01234567-89ab-cdef-0123-456789abcdef'
                """
            )
            assert row is not None
            assert row["shuffle_key"] == stable_shuffle_key(
                UUID("01234567-89ab-cdef-0123-456789abcdef")
            )
            assert row["heat_score"] is None
            assert await connection.fetchval(
                "SELECT to_regclass('public.source_ranking_snapshots')"
            ) == "source_ranking_snapshots"
            heat_index = await connection.fetchval(
                """
                SELECT indexdef
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND indexname = 'ix_works_heat_score_id'
                """
            )
            assert heat_index is not None
            assert "heat_score DESC NULLS LAST, id DESC" in heat_index
        finally:
            await connection.close()

    alembic("upgrade", "ff57a91bcd35")
    asyncio.run(prepare_and_assert())
    alembic("upgrade", "head")
    asyncio.run(assert_upgraded())
    alembic("downgrade", "ff57a91bcd35")
    alembic("upgrade", "head")
    asyncio.run(assert_upgraded())
