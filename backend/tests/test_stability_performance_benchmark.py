"""Safety and scale contracts for the stability performance benchmark."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml


def test_scale_profile_matches_release_acceptance_shape():
    from scripts import benchmark_stability_hot_paths as benchmark

    assert benchmark.DEFAULT_INTENT_COUNT == 70_000
    assert benchmark.DEFAULT_ASSET_COUNT == 100_000
    assert benchmark.DEFAULT_REPOSITORY_COUNT == 804
    assert benchmark.PERFORMANCE_TARGETS_MS == {
        "outbox_readiness": 50.0,
        "dedup_candidates": 100.0,
        "import_job_delete": 200.0,
        "workbench_cached": 500.0,
        "gitllery_status": 200.0,
    }


def test_synthetic_mode_requires_explicit_disposable_confirmation():
    from scripts import benchmark_stability_hot_paths as benchmark

    with pytest.raises(RuntimeError, match="--confirm-disposable"):
        benchmark.assert_synthetic_database_allowed(
            "postgresql+asyncpg://user:secret@127.0.0.1:5432/autogallery",
            confirmed=False,
            allow_non_loopback=False,
        )


def test_synthetic_mode_rejects_remote_database_without_second_guard():
    from scripts import benchmark_stability_hot_paths as benchmark

    with pytest.raises(RuntimeError, match="--allow-non-loopback-disposable"):
        benchmark.assert_synthetic_database_allowed(
            "postgresql+asyncpg://user:secret@postgres:5432/autogallery",
            confirmed=True,
            allow_non_loopback=False,
        )


@pytest.mark.asyncio
async def test_live_measurement_starts_with_a_read_only_transaction():
    from scripts import benchmark_stability_hot_paths as benchmark

    session = SimpleNamespace(execute=AsyncMock())

    await benchmark.enforce_read_only_transaction(session)

    statement = str(session.execute.await_args.args[0])
    assert statement == "SET TRANSACTION READ ONLY"


def test_percentile_uses_nearest_rank_for_acceptance_thresholds():
    from scripts import benchmark_stability_hot_paths as benchmark

    assert benchmark.percentile([1.0, 2.0, 3.0, 4.0, 100.0], 0.95) == 100.0
    assert benchmark.percentile([4.0, 1.0, 3.0, 2.0], 0.50) == 2.0


def test_gitllery_status_config_read_never_creates_a_missing_file(tmp_path):
    from app.services.settings import read_gallerydl_config

    config_path = tmp_path / "missing" / "config.json"

    config = read_gallerydl_config(config_path)

    assert config["extractor"]
    assert not config_path.exists()


def test_acceptance_performance_phase_runs_stability_hot_path_benchmark():
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "run-acceptance.sh"
    ).read_text()

    assert "benchmark_stability_hot_paths.py" in script
    assert "work-search-current.json" in script
    assert "work-search-baseline.json" in script
    assert "--require-baseline" in script
    assert "--sql-budget 6" in script
    assert "--assets 100000" in script
    assert "--intents 70000" in script


def test_compose_passes_the_verified_gitllery_generation_to_every_app_process():
    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "docker-compose.yaml").read_text()
    )
    expected = "${GITLLERY_ACTIVE_VERIFIED_GENERATION:-}"
    app_processes = (
        "backend",
        "worker-download",
        "worker-import",
        "worker-operations",
        "worker-discovery",
        "scheduler",
    )

    for service in app_processes:
        assert (
            compose["services"][service]["environment"]["GITLLERY_ACTIVE_VERIFIED_GENERATION"]
            == expected
        )
