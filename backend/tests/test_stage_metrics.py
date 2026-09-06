from __future__ import annotations

import pytest

from app.services import stage_metrics


def test_measure_stage_reports_bounded_counters(monkeypatch):
    io_samples = iter(((100, 200), (140, 260)))
    records = []
    monkeypatch.setattr(stage_metrics, "_process_io", lambda: next(io_samples))
    monkeypatch.setattr(stage_metrics, "_rss_peak_bytes", lambda: 1234)
    monkeypatch.setattr(
        stage_metrics.logger,
        "info",
        lambda _message, *, extra: records.append(extra["stage_metrics"]),
    )

    with stage_metrics.measure_stage("unit", batch=25) as payload:
        payload["processed"] = 24

    assert records == [payload]
    assert payload["outcome"] == "ok"
    assert payload["batch"] == 25
    assert payload["processed"] == 24
    assert payload["read_bytes"] == 40
    assert payload["write_bytes"] == 60
    assert payload["rss_peak_bytes"] == 1234
    assert payload["sql_count"] == 0
    assert payload["commit_count"] == 0


def test_measure_stage_logs_error(monkeypatch):
    records = []
    monkeypatch.setattr(stage_metrics, "_process_io", lambda: (0, 0))
    monkeypatch.setattr(stage_metrics, "_rss_peak_bytes", lambda: 0)
    monkeypatch.setattr(
        stage_metrics.logger,
        "info",
        lambda _message, *, extra: records.append(extra["stage_metrics"]),
    )

    with pytest.raises(RuntimeError, match="boom"):
        with stage_metrics.measure_stage("unit-error"):
            raise RuntimeError("boom")

    assert records[0]["outcome"] == "error"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_durable_commit_counter_excludes_savepoint_release():
    from sqlalchemy import text
    from app.database import async_session, engine

    try:
        with stage_metrics.measure_stage("commit_boundary") as payload:
            async with async_session() as db:
                async with db.begin_nested():
                    await db.execute(text("SELECT 1"))
                await db.commit()
        assert payload["commit_count"] == 1
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_parent_stage_accounts_for_nested_database_work():
    from sqlalchemy import text
    from app.database import async_session, engine

    try:
        with stage_metrics.measure_stage("download") as parent:
            with stage_metrics.measure_stage("registration") as child:
                async with async_session() as db:
                    await db.execute(text("SELECT 1"))
                    await db.commit()
        assert child["sql_count"] == parent["sql_count"] == 1
        assert child["commit_count"] == parent["commit_count"] == 1
    finally:
        await engine.dispose()
