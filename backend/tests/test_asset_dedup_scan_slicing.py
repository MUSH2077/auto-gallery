"""Static contracts for the durable full-library dedup scan coordinator."""

import inspect


def test_asset_scan_rq_job_runs_only_one_nonblocking_resource_slice():
    from app.jobs.asset_dedup import _run_scan

    source = inspect.getsource(_run_scan)
    assert "while True" not in source
    assert "wait_for_capacity=False" in source
    assert "cooldown_result=cooldown" in source
    assert ".with_for_update()" in source


def test_asset_scan_successor_is_delayed_unique_and_generation_fenced():
    from app.jobs.asset_dedup import (
        _enqueue_scan_successor,
        _reserve_scan_successor,
    )

    reserve_source = inspect.getsource(_reserve_scan_successor)
    enqueue_source = inspect.getsource(_enqueue_scan_successor)
    assert 'scan_options["_rq_generation"] = next_generation' in reserve_source
    assert "asset-dedup-scan-" in reserve_source
    assert "checked_enqueue_in" in enqueue_source
    assert "ASSET_DEDUP_SCAN_QUEUE" in enqueue_source
    assert "job_id=rq_job_id" in enqueue_source


def test_asset_scan_terminal_paths_release_only_the_owned_operation_lock():
    from app.jobs.asset_dedup import (
        _complete_scan_operation,
        _fail_scan_operation,
    )

    combined = "\n".join(
        (
            inspect.getsource(_complete_scan_operation),
            inspect.getsource(_fail_scan_operation),
        )
    )
    assert "release_owned_operation_lock" in combined
    assert "ASSET_DEDUP_SCAN_OPERATION_LOCK" in combined
