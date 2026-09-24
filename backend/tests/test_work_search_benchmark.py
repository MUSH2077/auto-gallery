"""Release-shape contracts for work search performance measurement."""

from __future__ import annotations


def test_work_search_benchmark_covers_release_scale_and_new_sorts():
    from scripts import benchmark_work_search as benchmark

    assert benchmark.SCALE_REQUIREMENTS["works"] == 70_000
    assert benchmark.NEW_SORT_TARGET_MS == 500.0
    assert benchmark.WORK_SEARCH_SQL_BUDGET == 6
    cases = {case.name: case for case in benchmark.BENCHMARK_CASES}
    assert {
        "created-desc",
        "created-asc",
        "posted-desc",
        "posted-asc",
        "updated-desc",
        "updated-asc",
        "title-desc",
        "title-asc",
    } <= cases.keys()
    assert cases["heat"].query == "sort:heat-desc"
    assert cases["heat"].cursor_page is True
    assert cases["random"].query == "sort:random"
    assert cases["random"].seed is not None
    assert cases["random"].cursor_page is True


def test_existing_sort_regression_gate_allows_at_most_ten_percent():
    from scripts import benchmark_work_search as benchmark

    baseline = {
        "default:first": 100.0,
        "posted-desc:first": 200.0,
        "updated-asc:first": 50.0,
        "title-asc:first": 300.0,
    }
    current = {
        "default:first": 110.0,
        "posted-desc:first": 220.01,
        "updated-asc:first": 54.0,
        "title-asc:first": 329.0,
        "heat:first": 499.0,
    }

    failures = benchmark.existing_sort_regressions(current, baseline)

    assert failures == {
        "posted-desc:first": {"baseline_ms": 200.0, "current_ms": 220.01}
    }
