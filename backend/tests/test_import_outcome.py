from app.jobs.import_outcome import classify_import_outcome, clamp_threshold


def test_zero_groups_is_failed():
    status, msg = classify_import_outcome(
        total_groups=0, works=0, assets=0, existing=0, skipped=0, threshold=0.5)
    assert status == "failed"
    assert "0" in msg


def test_all_existing_is_complete():
    status, _ = classify_import_outcome(
        total_groups=5, works=0, assets=0, existing=5, skipped=0, threshold=0.5)
    assert status == "complete"


def test_all_skipped_is_failed():
    status, _ = classify_import_outcome(
        total_groups=4, works=0, assets=0, existing=0, skipped=4, threshold=0.5)
    assert status == "failed"


def test_skip_rate_at_threshold_is_failed():
    status, _ = classify_import_outcome(
        total_groups=4, works=2, assets=2, existing=0, skipped=2, threshold=0.5)
    assert status == "failed"


def test_skip_rate_below_threshold_is_complete():
    status, _ = classify_import_outcome(
        total_groups=10, works=9, assets=9, existing=0, skipped=1, threshold=0.5)
    assert status == "complete"


def test_healthy_is_complete_and_summary_has_counts():
    status, msg = classify_import_outcome(
        total_groups=3, works=3, assets=7, existing=0, skipped=0, threshold=0.5)
    assert status == "complete"
    assert "works=3" in msg and "assets=7" in msg


def test_clamp_threshold():
    assert clamp_threshold(-1.0) == 0.0
    assert clamp_threshold(2.0) == 1.0
    assert clamp_threshold(0.5) == 0.5
