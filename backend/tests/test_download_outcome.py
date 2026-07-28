from app.jobs.download_outcome import classify_no_metadata_outcome


def test_auth_warning_empty_is_failed():
    status, msg = classify_no_metadata_outcome(
        image_count=0, auth_warning="login required", is_subscription=True)
    assert status == "failed"
    assert "login required" in msg


def test_manual_empty_is_failed():
    status, msg = classify_no_metadata_outcome(
        image_count=0, auth_warning=None, is_subscription=False)
    assert status == "failed"
    assert "no artifacts" in msg


def test_subscription_empty_is_complete():
    status, msg = classify_no_metadata_outcome(
        image_count=0, auth_warning=None, is_subscription=True)
    assert status == "complete"
    assert msg is not None  # explains "no new content"


def test_media_without_metadata_is_complete():
    status, msg = classify_no_metadata_outcome(
        image_count=3, auth_warning=None, is_subscription=False)
    assert status == "complete"
    assert msg is None
