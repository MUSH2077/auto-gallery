from types import SimpleNamespace


def _job(*, retry_count=4, manifest=None):
    return SimpleNamespace(retry_count=retry_count, manifest=manifest)


def test_exhausted_provider_failure_evidence_is_attempt_scoped():
    from app.services.download_failure_evidence import (
        clear_unresolved_provider_failure,
        record_unresolved_provider_failure,
        unresolved_provider_failure,
    )

    job = _job()
    record_unresolved_provider_failure(
        job,
        kind="nonzero",
        reason="gallery-dl provider returned 17",
        max_retries=4,
    )

    evidence = unresolved_provider_failure(job)
    assert evidence["kind"] == "nonzero"
    assert evidence["reason"] == "gallery-dl provider returned 17"
    assert evidence["retry_count"] == evidence["max_retries"] == 4

    job.retry_count = 0
    assert unresolved_provider_failure(job) is None
    clear_unresolved_provider_failure(job)
    assert "unresolved_provider_failure" not in job.manifest


def test_arbitrary_manifest_text_is_not_provider_failure_evidence():
    from app.services.download_failure_evidence import unresolved_provider_failure

    job = _job(manifest={"error": "provider failed", "events": [{"event": "failed"}]})
    assert unresolved_provider_failure(job) is None


def test_zero_retry_authentication_failure_is_still_exhausted_evidence():
    from app.services.download_failure_evidence import (
        record_unresolved_provider_failure,
        unresolved_provider_failure,
    )

    job = _job(retry_count=0)
    record_unresolved_provider_failure(
        job,
        kind="authentication",
        reason="authentication failed",
        max_retries=0,
    )

    assert unresolved_provider_failure(job)["max_retries"] == 0
