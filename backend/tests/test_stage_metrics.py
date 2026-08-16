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
