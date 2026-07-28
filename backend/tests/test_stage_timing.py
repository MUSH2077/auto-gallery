from types import SimpleNamespace
from app.jobs.stage_timing import stage_timer


def test_stage_timer_appends_event():
    job = SimpleNamespace(manifest=None)
    with stage_timer(job, "scan"):
        pass
    events = job.manifest["events"]
    assert events[-1]["event"] == "stage_timing"
    assert events[-1]["stage"] == "scan"
    assert isinstance(events[-1]["ms"], int)
    assert events[-1]["ms"] >= 0
