import logging

import pytest


@pytest.mark.parametrize(
    ("outcome", "expected_fragment"),
    [
        ("replayed", "Enqueued auto_retry 1/4"),
        ("existing", "Enqueued auto_retry 1/4"),
        ("deferred", "Deferred auto_retry 1/4"),
        ("error", "Retry dispatch failed for auto_retry 1/4"),
        ("cancelled", "Retry dispatch not published for auto_retry 1/4"),
    ],
)
def test_retry_dispatch_log_matches_actual_publication_outcome(
    caplog,
    outcome,
    expected_fragment,
):
    """Deferred/cancelled durable work must not be reported as enqueued."""

    from app.jobs import download as download_job

    caplog.set_level(logging.INFO, logger=download_job.__name__)
    download_job._log_retry_dispatch_outcome(
        outcome,
        action="auto_retry",
        retry_count=1,
        max_retries=4,
        job_id="job-1",
        delay_seconds=60,
    )

    assert expected_fragment in caplog.text
    if outcome not in {"replayed", "existing"}:
        assert "Enqueued auto_retry" not in caplog.text
