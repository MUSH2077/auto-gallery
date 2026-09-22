from collections import deque
from datetime import UTC, date, datetime
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

    outcome = scheduler_loop.scheduler_watchdog()

    assert outcome["status"] == "scheduled"
    assert outcome["pixiv_ranking"] == {
        "status": "error",
        "error": "ranking queue unavailable",
    }
