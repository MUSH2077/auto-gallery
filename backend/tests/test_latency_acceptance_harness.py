"""Acceptance must reject evidence from stale or mixed experimental code."""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def harness():
    path = Path(__file__).resolve().parents[2] / "scripts/latency-acceptance.py"
    spec = importlib.util.spec_from_file_location("latency_acceptance_host", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evidence(harness):
    state = {"run_id": "fresh-run", "harness_hashes": harness.harness_hashes(),
             "revisions": {"baseline": "old-commit", "candidate": "new-commit"}}
    row = {"event": "trial", "variant": "candidate", "source_revision": "new-commit",
           "run_id": state["run_id"], "harness_sha256": state["harness_hashes"]["driver"],
           "orchestrator_sha256": state["harness_hashes"]["orchestrator"],
           "repetition": 1, "works": 9, "assets": 27}
    return state, row


def test_sealed_measurement_can_resume(harness):
    state, row = evidence(harness)
    harness.validate_record(state, row, variant="candidate", repetition=1, works=9, assets=27)


@pytest.mark.parametrize("field", ["harness_sha256", "orchestrator_sha256", "run_id", "source_revision"])
def test_stale_measurement_cannot_join_an_individually_matched_pair(harness, field):
    state, row = evidence(harness)
    row[field] = "stale-but-equal-within-pair"
    with pytest.raises(RuntimeError, match=field):
        harness.validate_record(state, row)


def test_resumed_measurement_must_match_the_requested_scenario(harness):
    state, row = evidence(harness)
    with pytest.raises(RuntimeError, match="assets"):
        harness.validate_record(state, row, variant="candidate", repetition=1, works=9, assets=38)


def test_changing_current_driver_requires_a_fresh_seal(harness, monkeypatch):
    state, row = evidence(harness)
    monkeypatch.setattr(harness, "harness_hashes", lambda: {"driver": "changed", "orchestrator": "changed"})
    with pytest.raises(RuntimeError, match="seal a fresh run"):
        harness.validate_record(state, row)


def test_unsealed_preliminary_state_cannot_be_used(harness):
    state, row = evidence(harness)
    state.pop("run_id")
    with pytest.raises(RuntimeError, match="unsealed"):
        harness.validate_record(state, row)
