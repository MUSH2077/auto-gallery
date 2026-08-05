from datetime import datetime, timezone

from app.services.sync_outcome import (
    build_sync_outcome,
    clear_download_job_outcome,
    download_job_outcome,
    set_download_job_outcome,
    sync_outcome_from_manifest,
)


class Job:
    manifest = None


def test_sync_outcome_round_trip_uses_stable_contract():
    completed_at = datetime(2026, 7, 31, 8, 30, tzinfo=timezone.utc)
    outcome = build_sync_outcome(
        "no_changes",
        metadata_count=0,
        media_count=0,
        completed_at=completed_at,
    )
    job = Job()
    set_download_job_outcome(job, outcome)

    assert download_job_outcome(job) == outcome
    assert job.manifest["outcome"]["completed_at"] == completed_at.isoformat()


def test_invalid_manifest_outcome_is_not_exposed():
    assert sync_outcome_from_manifest({"outcome": {"code": "complete"}}) is None
    assert sync_outcome_from_manifest({"outcome": "no_changes"}) is None


def test_clear_outcome_preserves_other_manifest_fields():
    job = Job()
    job.manifest = {"version": 1, "outcome": build_sync_outcome("no_content", metadata_count=0, media_count=0)}
    clear_download_job_outcome(job)
    assert job.manifest == {"version": 1}
