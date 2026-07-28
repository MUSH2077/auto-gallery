from types import SimpleNamespace
from uuid import uuid4

from app.services.job_progress import (
    apply_download_progress,
    apply_import_progress,
    import_progress_from_job,
    make_progress,
)


def test_make_progress_calculates_percent_from_counts():
    progress = make_progress("importing", current=2, total=4)

    assert progress["stage"] == "importing"
    assert progress["current"] == 2
    assert progress["total"] == 4
    assert progress["percent"] == 50.0


def test_apply_download_progress_sets_persistent_fields():
    job = SimpleNamespace(id=uuid4(), pipeline_stage=None, progress_data=None)

    progress = apply_download_progress(
        job,
        "enqueued",
        "Queued; waiting for download worker",
        publish=False,
    )

    assert job.pipeline_stage == "enqueued"
    assert job.progress_data == progress
    assert job.progress_data["message"] == "Queued; waiting for download worker"


def test_apply_import_progress_sets_persistent_fields():
    job = SimpleNamespace(
        id=uuid4(),
        progress_stage=None,
        progress_works_done=None,
        progress_works_total=None,
        progress_data=None,
    )

    progress = apply_import_progress(
        job,
        "importing",
        "Importing work 1 of 2",
        current=1,
        total=2,
        publish=False,
    )

    assert job.progress_stage == "importing"
    assert job.progress_works_done == 1
    assert job.progress_works_total == 2
    assert job.progress_data == progress


def test_import_progress_from_job_falls_back_to_structured_fields():
    job = SimpleNamespace(
        progress_data=None,
        progress_stage="importing",
        progress_works_done=1,
        progress_works_total=2,
    )

    progress = import_progress_from_job(job)

    assert progress == {
        "stage": "importing",
        "current": 1,
        "total": 2,
        "percent": 50.0,
    }
