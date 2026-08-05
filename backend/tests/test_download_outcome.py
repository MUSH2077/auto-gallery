from app.jobs.download_outcome import classify_no_metadata_outcome


def test_auth_warning_empty_is_failed():
    decision = classify_no_metadata_outcome(
        image_count=0, auth_warning="login required", is_subscription=True)
    assert decision.status == "failed"
    assert "login required" in decision.error
    assert decision.outcome_code is None


def test_manual_empty_is_failed():
    decision = classify_no_metadata_outcome(
        image_count=0, auth_warning=None, is_subscription=False)
    assert decision.status == "failed"
    assert "no artifacts" in decision.error
    assert decision.outcome_code is None


def test_first_subscription_empty_is_complete_without_content():
    decision = classify_no_metadata_outcome(
        image_count=0, auth_warning=None, is_subscription=True)
    assert decision.status == "complete"
    assert decision.error is None
    assert decision.outcome_code == "no_content"


def test_subscription_resync_empty_is_complete_without_changes():
    decision = classify_no_metadata_outcome(
        image_count=0,
        auth_warning=None,
        is_subscription=True,
        had_sync_baseline=True,
    )
    assert decision.status == "complete"
    assert decision.error is None
    assert decision.outcome_code == "no_changes"


def test_media_without_metadata_is_complete():
    decision = classify_no_metadata_outcome(
        image_count=3, auth_warning=None, is_subscription=False)
    assert decision.status == "complete"
    assert decision.error is None
    assert decision.outcome_code is None
