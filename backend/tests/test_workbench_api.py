from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, *, scalar_value=None, rows=None, scalar_rows=None):
        self._scalar_value = scalar_value
        self._rows = rows or []
        self._scalar_rows = scalar_rows or []

    def scalar(self):
        return self._scalar_value

    def all(self):
        return self._rows

    def scalars(self):
        return _Scalars(self._scalar_rows)


class _Session:
    def __init__(self, results):
        self._results = iter(results)
        self.execute_count = 0

    async def execute(self, _statement):
        self.execute_count += 1
        return next(self._results)


@pytest.mark.asyncio
async def test_workbench_refresh_populates_cache_and_exposes_recent_context(monkeypatch):
    from app.api import system

    now = datetime(2026, 7, 28, 8, 30, tzinfo=timezone.utc)
    creator = SimpleNamespace(id=uuid4(), display_name="Atlas Ink", name="atlas")
    subscription = SimpleNamespace(id=uuid4(), name="Atlas archive")
    download = SimpleNamespace(
        id=uuid4(),
        subscription_id=subscription.id,
        subscription_source_id=uuid4(),
        source="pixiv",
        source_url="https://www.pixiv.net/users/10000001",
        status="downloading",
        pipeline_stage="download",
        progress_data={"current": 48, "total": 132, "percent": 36.4},
        created_at=now,
        updated_at=now,
        error_log=None,
    )
    import_job = SimpleNamespace(
        id=uuid4(),
        download_job_id=download.id,
        status="running",
        progress_stage="works",
        progress_works_done=8,
        progress_works_total=12,
        progress_data={"current": 8, "total": 12, "percent": 66.7},
        created_at=now,
        updated_at=now,
        error_log=None,
    )
    work = SimpleNamespace(
        id=uuid4(),
        title="Harbor Light Study",
        thumbnail_asset_id=uuid4(),
        created_at=now,
    )
    source = SimpleNamespace(id=uuid4(), source="pixiv", source_url=download.source_url, last_synced_at=now)

    session = _Session([
        _Result(scalar_value=0),
        _Result(scalar_value=0),
        _Result(scalar_value=0),
        _Result(rows=[(download, subscription, creator)]),
        _Result(rows=[(import_job, download, subscription, creator)]),
        _Result(scalar_rows=[work]),
        _Result(scalar_rows=[work.id]),
        _Result(rows=[(work.id, "pixiv", creator.display_name, creator.name)]),
        _Result(rows=[(source, subscription, creator)]),
    ])

    monkeypatch.setattr(system, "_workbench_cache", {"cached": True})
    monkeypatch.setattr(system, "_workbench_cache_ts", system.time.monotonic())
    monkeypatch.setattr(
        system.os,
        "statvfs",
        lambda _path: SimpleNamespace(f_frsize=1, f_blocks=1000, f_bavail=620),
    )
    monkeypatch.setattr(system, "_queue_stats_payload", AsyncMock(return_value={
        "default_queue": 2,
        "scheduled_queue": 1,
        "failed_jobs": 1,
        "started_jobs": 2,
        "next_sync_scan_at": now.isoformat(),
    }))
    monkeypatch.setattr(system, "get_scheduler_config", AsyncMock(return_value={
        "scheduler_enabled": True,
        "schedule_mode": "interval",
        "timezone": "Asia/Shanghai",
        "scheduler_scan_interval_minutes": 15,
    }))
    monkeypatch.setattr(system, "get_download_defaults", AsyncMock(return_value={"timeout_seconds": 600}))
    monkeypatch.setattr(system, "_count_statuses", AsyncMock(side_effect=[1, 0, 1, 0]))
    monkeypatch.setattr(system, "_count_active_rebuilds", AsyncMock(return_value=0))
    monkeypatch.setattr(system, "_get_proxy_health_summary", AsyncMock(return_value={}))
    monkeypatch.setattr(system, "_quick_service_health", AsyncMock(return_value={"backend": "up"}))

    payload = await system.workbench_summary(refresh=True, db=session)

    assert payload["queue"]["active_download_count"] == 1
    assert payload["recent"]["download_jobs"][0]["creator_name"] == "Atlas Ink"
    assert payload["recent"]["download_jobs"][0]["progress_data"]["current"] == 48
    assert payload["recent"]["import_jobs"][0]["source"] == "pixiv"
    assert payload["recent"]["import_jobs"][0]["progress_works_total"] == 12
    assert payload["recent"]["works"][0]["source"] == "pixiv"
    assert payload["recent"]["works"][0]["creator_name"] == "Atlas Ink"
    assert payload["recent"]["works"][0]["has_video"] is True
    assert system._workbench_cache is payload
    assert system._WORKBENCH_CACHE_TTL == 10.0

    cached = await system.workbench_summary(refresh=False, db=SimpleNamespace())
    assert cached is payload
    assert session.execute_count == 9
