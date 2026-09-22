from collections import deque
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest


class FixtureTransport:
    """Deterministic Pixiv boundary double with complete response envelopes."""

    def __init__(self, *responses):
        self.responses = deque(responses)
        self.requests = []

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected remote request")
        return self.responses.popleft()


def _ranking_illust(work_id: int) -> dict:
    return {
        "id": work_id,
        "title": f"Ranking fixture {work_id}",
        "type": "illust",
        "image_urls": {
            "square_medium": f"https://i.pximg.net/c/360x360/{work_id}_p0.jpg",
            "medium": f"https://i.pximg.net/c/540x540/{work_id}_p0.jpg",
            "large": f"https://i.pximg.net/img-master/{work_id}_p0.jpg",
        },
        "caption": "fixture caption",
        "restrict": 0,
        "user": {
            "id": 123,
            "name": "Pixiv Artist",
            "account": "pixiv_artist",
            "profile_image_urls": {"medium": "https://i.pximg.net/avatar.jpg"},
            "comment": "fixture profile",
            "is_followed": True,
        },
        "tags": [],
        "tools": [],
        "create_date": "2026-09-20T10:20:30+09:00",
        "page_count": 1,
        "width": 1200,
        "height": 900,
        "sanity_level": 2,
        "x_restrict": 0,
        "series": None,
        "meta_single_page": {},
        "meta_pages": [],
        "total_view": 100,
        "total_bookmarks": 10,
        "is_bookmarked": False,
        "visible": True,
        "is_muted": False,
        "illust_ai_type": 1,
        "illust_book_style": 0,
    }


@pytest.mark.asyncio
async def test_pixiv_ranking_fetches_bounded_pages_with_stable_rank_order():
    from app.remote_discovery.common import RemoteHTTPResponse
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter

    transport = FixtureTransport(
        RemoteHTTPResponse(200, {"access_token": "access", "expires_in": 3600}, {}),
        RemoteHTTPResponse(
            200,
            {
                "illusts": [_ranking_illust(101), _ranking_illust(102)],
                "next_url": (
                    "https://app-api.pixiv.net/v1/illust/ranking"
                    "?filter=for_ios&mode=day&date=2026-09-21&offset=2"
                ),
            },
            {},
        ),
        RemoteHTTPResponse(
            200,
            {"illusts": [_ranking_illust(103), _ranking_illust(104)], "next_url": None},
            {},
        ),
    )

    result = await PixivRemoteDiscoveryAdapter(transport).fetch_rankings(
        {"refresh_token": "refresh"},
        mode="day",
        ranking_date=date(2026, 9, 21),
        limit=3,
    )

    assert result.mode == "day"
    assert result.ranking_date == date(2026, 9, 21)
    assert [(item.source_work_id, item.rank) for item in result.items] == [
        ("101", 1),
        ("102", 2),
        ("103", 3),
    ]
    assert len(transport.requests) == 3
    first_request = transport.requests[1]
    second_request = transport.requests[2]
    assert first_request[:2] == (
        "GET",
        "https://app-api.pixiv.net/v1/illust/ranking",
    )
    assert first_request[2]["params"] == {
        "filter": "for_ios",
        "mode": "day",
        "date": "2026-09-21",
        "offset": 0,
    }
    assert second_request[2]["params"]["offset"] == 2


@pytest.mark.asyncio
async def test_pixiv_ranking_rejects_untrusted_next_page_and_invalid_modes():
    from app.remote_discovery.common import MalformedRemoteResponse, RemoteHTTPResponse
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter

    with pytest.raises(ValueError, match="ranking mode"):
        await PixivRemoteDiscoveryAdapter(FixtureTransport()).fetch_rankings(
            {"refresh_token": "refresh"},
            mode="week",
            ranking_date=date(2026, 9, 21),
        )

    transport = FixtureTransport(
        RemoteHTTPResponse(200, {"access_token": "access"}, {}),
        RemoteHTTPResponse(
            200,
            {
                "illusts": [_ranking_illust(101)],
                "next_url": "https://attacker.invalid/v1/illust/ranking?offset=1",
            },
            {},
        ),
    )
    with pytest.raises(MalformedRemoteResponse, match="next_url"):
        await PixivRemoteDiscoveryAdapter(transport).fetch_rankings(
            {"refresh_token": "refresh"},
            mode="day_ai",
            ranking_date=date(2026, 9, 21),
        )


def test_pixiv_ranking_schedule_uses_jst_publication_cutoff_and_legal_job_id():
    from app.services.pixiv_ranking_scheduler import (
        PIXIV_RANKING_MODES,
        pixiv_heat_expiry_job_id,
        pixiv_ranking_job_id,
        pixiv_ranking_plan,
    )

    before_cutoff = pixiv_ranking_plan(datetime(2026, 9, 22, 3, 14, tzinfo=UTC))
    after_cutoff = pixiv_ranking_plan(datetime(2026, 9, 22, 3, 15, tzinfo=UTC))

    assert PIXIV_RANKING_MODES == ("day", "day_ai", "day_r18", "day_r18_ai")
    assert before_cutoff.ranking_date == date(2026, 9, 20)
    assert after_cutoff.ranking_date == date(2026, 9, 21)
    assert before_cutoff.ready_at == datetime(2026, 9, 21, 3, 15, tzinfo=UTC)
    assert after_cutoff.ready_at == datetime(2026, 9, 22, 3, 15, tzinfo=UTC)
    assert pixiv_ranking_job_id(after_cutoff.ranking_date) == "pixiv-ranking-2026-09-21"
    assert ":" not in pixiv_ranking_job_id(after_cutoff.ranking_date)
    expiry_job_id = pixiv_heat_expiry_job_id(datetime(2026, 9, 24, 3, 15, 1, tzinfo=UTC))
    assert expiry_job_id == "pixiv-heat-expiry-1790219701"
    assert ":" not in expiry_job_id


def test_pixiv_heat_expiry_schedules_one_guarded_job_at_the_48_hour_boundary(monkeypatch):
    from rq.exceptions import NoSuchJobError

    from app.services import pixiv_ranking_scheduler

    fetched_at = datetime(2026, 9, 22, 3, 15, tzinfo=UTC)
    now = fetched_at + timedelta(hours=1)
    redis = object()
    queue = object()
    calls = []

    def fetch(_job_id, *, connection):
        assert connection is redis
        raise NoSuchJobError

    def enqueue_in(actual_queue, delay, function, **kwargs):
        calls.append((actual_queue, delay, function, kwargs))

    monkeypatch.setattr(pixiv_ranking_scheduler.Job, "fetch", fetch)
    monkeypatch.setattr(pixiv_ranking_scheduler, "Queue", lambda **_kwargs: queue)
    monkeypatch.setattr(pixiv_ranking_scheduler, "checked_enqueue_in", enqueue_in)

    outcome = pixiv_ranking_scheduler.schedule_pixiv_heat_expiry(
        fetched_at,
        now=now,
        redis_client=redis,
    )

    assert outcome["created"] is True
    assert outcome["expires_at"] == "2026-09-24T03:15:01+00:00"
    assert calls[0][0] is queue
    assert calls[0][1] == timedelta(hours=47, seconds=1)
    assert calls[0][3]["job_id"] == outcome["job_id"]

    existing = SimpleNamespace(get_status=lambda refresh: "scheduled")
    monkeypatch.setattr(
        pixiv_ranking_scheduler.Job,
        "fetch",
        lambda _job_id, *, connection: existing,
    )
    repeated = pixiv_ranking_scheduler.schedule_pixiv_heat_expiry(
        fetched_at,
        now=now,
        redis_client=redis,
    )
    assert repeated["created"] is False
    assert len(calls) == 1


def test_pixiv_heat_expiry_replaces_a_terminal_job_instead_of_losing_the_check(
    monkeypatch,
):
    from app.services import pixiv_ranking_scheduler

    fetched_at = datetime(2026, 9, 22, 3, 15, tzinfo=UTC)
    redis = object()
    queue = object()
    calls = []

    class FailedJob:
        deleted = False

        def get_status(self, refresh=True):
            assert refresh is True
            return "failed"

        def delete(self):
            self.deleted = True

    existing = FailedJob()
    monkeypatch.setattr(
        pixiv_ranking_scheduler.Job,
        "fetch",
        lambda _job_id, *, connection: existing,
    )
    monkeypatch.setattr(pixiv_ranking_scheduler, "Queue", lambda **_kwargs: queue)
    monkeypatch.setattr(
        pixiv_ranking_scheduler,
        "checked_enqueue_in",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    outcome = pixiv_ranking_scheduler.schedule_pixiv_heat_expiry(
        fetched_at,
        now=fetched_at,
        redis_client=redis,
    )

    assert existing.deleted is True
    assert outcome["created"] is True
    assert len(calls) == 1


def test_pixiv_heat_expiry_does_not_repeat_an_already_completed_check(monkeypatch):
    from app.services import pixiv_ranking_scheduler

    fetched_at = datetime(2026, 9, 22, 3, 15, tzinfo=UTC)
    redis = object()

    class FinishedJob:
        def get_status(self, refresh=True):
            assert refresh is True
            return "finished"

        def delete(self):
            raise AssertionError("a completed expiry check must remain satisfied")

    monkeypatch.setattr(
        pixiv_ranking_scheduler.Job,
        "fetch",
        lambda _job_id, *, connection: FinishedJob(),
    )

    outcome = pixiv_ranking_scheduler.schedule_pixiv_heat_expiry(
        fetched_at,
        now=fetched_at + timedelta(days=3),
        redis_client=redis,
    )

    assert outcome["created"] is False
    assert outcome["status"] == "finished"


def test_latest_pixiv_heat_expiry_reconciliation_uses_the_database_snapshot(
    monkeypatch,
):
    from app.services import pixiv_ranking_scheduler

    fetched_at = datetime(2026, 9, 22, 3, 15, tzinfo=UTC)
    redis = object()
    scheduled = []

    def run_latest(awaitable):
        if hasattr(awaitable, "close"):
            awaitable.close()
        return fetched_at

    monkeypatch.setattr(pixiv_ranking_scheduler.asyncio, "run", run_latest)
    monkeypatch.setattr(
        pixiv_ranking_scheduler,
        "schedule_pixiv_heat_expiry",
        lambda observed_at, *, redis_client: scheduled.append(
            (observed_at, redis_client)
        )
        or {"created": True, "status": "scheduled"},
    )

    outcome = pixiv_ranking_scheduler.ensure_latest_pixiv_heat_expiry(
        redis_client=redis
    )

    assert scheduled == [(fetched_at, redis)]
    assert outcome == {"created": True, "status": "scheduled"}


def test_pixiv_heat_expiry_job_ignores_newer_snapshots_and_queues_stale_ones(monkeypatch):
    from app.jobs import work_heat

    current = datetime(2026, 9, 24, 3, 15, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current if tz is not None else current.replace(tzinfo=None)

    latest_fetched_at = current - timedelta(hours=47, minutes=59)

    def run_latest(awaitable):
        if hasattr(awaitable, "close"):
            awaitable.close()
        return latest_fetched_at

    queued = []
    monkeypatch.setattr(work_heat, "datetime", FixedDateTime)
    monkeypatch.setattr(work_heat.asyncio, "run", run_latest)
    monkeypatch.setattr(
        work_heat,
        "request_work_heat_recompute",
        lambda sources: queued.append(sources)
        or {"created": 1, "coalesced": 0, "errors": 0},
    )

    fresh = work_heat.expire_pixiv_heat()
    assert fresh["status"] == "skipped"
    assert fresh["reason"] == "newer_ranking_is_fresh"
    assert queued == []

    latest_fetched_at = current - timedelta(hours=48, minutes=1)
    stale = work_heat.expire_pixiv_heat()
    assert stale["status"] == "queued"
    assert queued == [{"pixiv"}]


def test_pixiv_heat_expiry_job_raises_when_recompute_cannot_be_queued(monkeypatch):
    from app.jobs import work_heat

    current = datetime(2026, 9, 24, 3, 15, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current if tz is not None else current.replace(tzinfo=None)

    def run_latest(awaitable):
        if hasattr(awaitable, "close"):
            awaitable.close()
        return current - timedelta(hours=49)

    monkeypatch.setattr(work_heat, "datetime", FixedDateTime)
    monkeypatch.setattr(work_heat.asyncio, "run", run_latest)
    monkeypatch.setattr(
        work_heat,
        "request_work_heat_recompute",
        lambda _sources: {"created": 0, "coalesced": 0, "errors": 1},
    )

    with pytest.raises(RuntimeError, match="could not be queued"):
        work_heat.expire_pixiv_heat()


@pytest.mark.asyncio
async def test_ranking_snapshots_replace_all_four_modes_then_recompute_once():
    from app.remote_discovery.pixiv import (
        PIXIV_RANKING_MODES,
        PixivRankingEntry,
        PixivRankingResult,
    )
    from app.services.pixiv_ranking_sync import store_pixiv_ranking_results

    observed_at = datetime(2026, 9, 22, 3, 16, tzinfo=UTC)
    results = tuple(
        PixivRankingResult(
            mode=mode,
            ranking_date=date(2026, 9, 21),
            items=(PixivRankingEntry(f"{index}01", 1), PixivRankingEntry(f"{index}02", 2)),
            fetched_at=observed_at,
        )
        for index, mode in enumerate(PIXIV_RANKING_MODES, start=1)
    )

    class FakeSession:
        def __init__(self):
            self.statements = []
            self.added = []
            self.flush_count = 0

        async def execute(self, statement):
            self.statements.append(statement)

        def add_all(self, rows):
            self.added.extend(rows)

        async def flush(self):
            self.flush_count += 1

    recomputes = []

    async def recompute(db, sources, *, now):
        recomputes.append((db, sources, now))
        return {"changed-work"}

    db = FakeSession()
    outcome = await store_pixiv_ranking_results(db, results, recompute=recompute)

    assert len(db.statements) == 1
    assert len(db.added) == 8
    assert {row.mode for row in db.added} == set(PIXIV_RANKING_MODES)
    assert {row.rank_total for row in db.added} == {2}
    assert all(row.source == "pixiv" for row in db.added)
    assert db.flush_count == 1
    assert recomputes == [(db, {"pixiv"}, observed_at)]
    assert outcome == {"snapshots": 8, "changed_works": 1}


@pytest.mark.asyncio
async def test_ranking_snapshot_store_rejects_partial_or_empty_daily_batch():
    from app.remote_discovery.pixiv import PixivRankingResult
    from app.services.pixiv_ranking_sync import PixivRankingDateNotReady, store_pixiv_ranking_results

    partial = (
        PixivRankingResult(
            mode="day",
            ranking_date=date(2026, 9, 21),
            items=(),
            fetched_at=datetime(2026, 9, 22, 3, 16, tzinfo=UTC),
        ),
    )
    with pytest.raises(PixivRankingDateNotReady):
        await store_pixiv_ranking_results(SimpleNamespace(), partial)


def test_ranking_job_retries_not_ready_date_after_30_then_120_minutes(monkeypatch):
    from rq import Retry

    from app.jobs import pixiv_ranking_sync
    from app.services.pixiv_ranking_sync import PixivRankingDateNotReady

    def raise_not_ready(_awaitable):
        if hasattr(_awaitable, "close"):
            _awaitable.close()
        raise PixivRankingDateNotReady("ranking date has not advanced")

    monkeypatch.setattr(pixiv_ranking_sync.asyncio, "run", raise_not_ready)
    recomputes = []
    monkeypatch.setattr(
        pixiv_ranking_sync,
        "request_work_heat_recompute",
        lambda sources: recomputes.append(sources),
        raising=False,
    )
    retry = pixiv_ranking_sync.sync_pixiv_rankings("2026-09-21")

    assert isinstance(retry, Retry)
    assert retry.max == 2
    assert retry.intervals == [1800, 7200]
    assert recomputes == [{"pixiv"}]


@pytest.mark.asyncio
async def test_successful_ranking_sync_schedules_heat_expiry_after_commit(monkeypatch):
    from app.jobs import pixiv_ranking_sync
    from app.remote_discovery.pixiv import PIXIV_RANKING_MODES

    fetched_at = datetime(2026, 9, 22, 3, 16, tzinfo=UTC)
    db = SimpleNamespace(
        commit_count=0,
        commit=None,
    )

    async def commit():
        db.commit_count += 1

    db.commit = commit

    class SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return False

    async def healthy(_db):
        return SimpleNamespace(user_id="user")

    class Adapter:
        async def fetch_rankings(self, _credentials, *, mode, ranking_date):
            return SimpleNamespace(
                mode=mode,
                ranking_date=ranking_date,
                fetched_at=fetched_at,
                items=(SimpleNamespace(source_work_id=f"{mode}-1", rank=1),),
            )

    async def store(_db, results):
        assert tuple(result.mode for result in results) == PIXIV_RANKING_MODES
        return {"snapshots": 4, "changed_works": 1}

    scheduled = []
    monkeypatch.setattr(pixiv_ranking_sync, "async_session", lambda: SessionContext())
    monkeypatch.setattr(pixiv_ranking_sync, "_healthy_pixiv_account", healthy)
    monkeypatch.setattr(
        pixiv_ranking_sync,
        "RemoteAccountService",
        lambda _db, _user_id: SimpleNamespace(
            credentials_for_adapter=lambda _account: {"refresh_token": "fixture"}
        ),
    )
    monkeypatch.setattr(pixiv_ranking_sync, "PixivRemoteDiscoveryAdapter", Adapter)
    monkeypatch.setattr(pixiv_ranking_sync.registry, "get", lambda _source: Adapter())
    monkeypatch.setattr(pixiv_ranking_sync, "store_pixiv_ranking_results", store)
    monkeypatch.setattr(
        pixiv_ranking_sync,
        "_schedule_pixiv_heat_expiry",
        lambda observed_at: scheduled.append(observed_at),
    )

    outcome = await pixiv_ranking_sync.sync_pixiv_rankings_async("2026-09-21")

    assert db.commit_count == 2
    assert scheduled == [fetched_at]
    assert outcome == {
        "status": "completed",
        "ranking_date": "2026-09-21",
        "snapshots": 4,
        "changed_works": 1,
    }


def test_successful_ranking_sync_propagates_heat_expiry_schedule_failure(monkeypatch):
    from app.jobs import pixiv_ranking_sync

    def fail(_fetched_at):
        raise RuntimeError("expiry queue unavailable")

    monkeypatch.setattr(pixiv_ranking_sync, "schedule_pixiv_heat_expiry", fail)

    with pytest.raises(RuntimeError, match="expiry queue unavailable"):
        pixiv_ranking_sync._schedule_pixiv_heat_expiry(
            datetime(2026, 9, 22, 3, 16, tzinfo=UTC)
        )


def test_ranking_job_requests_expiry_recompute_when_no_account_is_available(monkeypatch):
    from app.jobs import pixiv_ranking_sync

    def return_skipped(awaitable):
        if hasattr(awaitable, "close"):
            awaitable.close()
        return {"status": "skipped", "reason": "no_healthy_account"}

    recomputes = []
    monkeypatch.setattr(pixiv_ranking_sync.asyncio, "run", return_skipped)
    monkeypatch.setattr(
        pixiv_ranking_sync,
        "request_work_heat_recompute",
        lambda sources: recomputes.append(sources),
        raising=False,
    )

    outcome = pixiv_ranking_sync.sync_pixiv_rankings("2026-09-21")

    assert outcome == {"status": "skipped", "reason": "no_healthy_account"}
    assert recomputes == [{"pixiv"}]


def test_ranking_job_degradation_is_not_masked_by_heat_queue_failure(monkeypatch):
    from app.jobs import pixiv_ranking_sync

    def return_skipped(awaitable):
        if hasattr(awaitable, "close"):
            awaitable.close()
        return {"status": "skipped", "reason": "no_healthy_account"}

    monkeypatch.setattr(pixiv_ranking_sync.asyncio, "run", return_skipped)
    monkeypatch.setattr(
        pixiv_ranking_sync,
        "request_work_heat_recompute",
        lambda _sources: (_ for _ in ()).throw(RuntimeError("queue unavailable")),
        raising=False,
    )

    assert pixiv_ranking_sync.sync_pixiv_rankings("2026-09-21") == {
        "status": "skipped",
        "reason": "no_healthy_account",
    }


def test_scheduler_watchdog_keeps_subscription_loop_when_ranking_ensure_fails(monkeypatch):
    from app.services import scheduler_loop

    redis = SimpleNamespace(hgetall=lambda _key: {})
    monkeypatch.setattr(scheduler_loop, "get_redis", lambda: redis)
    monkeypatch.setattr(
        scheduler_loop,
        "ensure_next_subscription_scan",
        lambda *_args, **_kwargs: {"created": False, "status": "scheduled"},
    )

    def fail_ranking(*_args, **_kwargs):
        raise RuntimeError("ranking queue unavailable")

    monkeypatch.setattr(scheduler_loop, "ensure_pixiv_ranking_sync", fail_ranking)
    expiry_reconciliations = []
    monkeypatch.setattr(
        scheduler_loop,
        "ensure_latest_pixiv_heat_expiry",
        lambda *, redis_client: expiry_reconciliations.append(redis_client)
        or {"created": True, "status": "scheduled"},
    )
    heat_queue_reconciliations = []
    monkeypatch.setattr(
        scheduler_loop,
        "reconcile_deferred_work_heat_jobs",
        lambda *, redis_client: heat_queue_reconciliations.append(redis_client)
        or {"checked": 1, "recovered": 1, "waiting": 0, "errors": 0},
    )

    outcome = scheduler_loop.scheduler_watchdog()

    assert outcome["status"] == "scheduled"
    assert outcome["pixiv_ranking"] == {
        "status": "error",
        "error": "ranking queue unavailable",
    }
    assert outcome["pixiv_heat_expiry"] == {
        "created": True,
        "status": "scheduled",
    }
    assert expiry_reconciliations == [redis]
    assert outcome["work_heat_queue"] == {
        "checked": 1,
        "recovered": 1,
        "waiting": 0,
        "errors": 0,
    }
    assert heat_queue_reconciliations == [redis]
