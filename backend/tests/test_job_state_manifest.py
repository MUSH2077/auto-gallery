import pytest

from app.services.job_manifest import append_manifest_event, redacted_manifest_config, update_manifest
from app.services.job_state import InvalidJobTransition, transition_download_job, transition_import_job


class DummyJob:
    def __init__(self, status="pending"):
        self.status = status
        self.error_log = None
        self.manifest = None


def test_download_state_machine_allows_partial_recovery_path():
    job = DummyJob("failed")
    transition_download_job(job, "importing", "partial recovery")
    assert job.status == "importing"
    assert job.error_log == "partial recovery"


def test_download_state_machine_rejects_invalid_transition():
    job = DummyJob("complete")
    with pytest.raises(InvalidJobTransition):
        transition_download_job(job, "downloading")


def test_import_state_machine_retry_flow():
    job = DummyJob("failed")
    transition_import_job(job, "pending")
    assert job.status == "enqueued"


def test_manifest_redacts_sensitive_config_and_appends_events():
    job = DummyJob()
    update_manifest(job, effective_gallerydl_config={
        "extractor": {"twitter": {"cookies": "/secret/cookies.txt", "strategy": "tweets"}},
        "token": "secret-token",
    })
    append_manifest_event(job, "created", password="pw", source="x")
    assert job.manifest["effective_gallerydl_config"]["extractor"]["twitter"]["cookies"] == "***REDACTED***"
    assert job.manifest["effective_gallerydl_config"]["token"] == "***REDACTED***"
    assert job.manifest["events"][0]["password"] == "***REDACTED***"


def test_redacted_manifest_config_recurses():
    cfg = redacted_manifest_config({"a": [{"api_key": "abc", "ok": 1}]})
    assert cfg["a"][0]["api_key"] == "***REDACTED***"
    assert cfg["a"][0]["ok"] == 1
