from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest


def test_canary_selection_is_deterministic_and_deduplicated():
    from app.services.gitllery.builds import select_canary_repository_keys

    counts = {
        "repo-z": 1000,
        "repo-b": 10,
        "repo-a": 10,
        "repo-m": 100,
        "repo-c": 50,
    }

    assert select_canary_repository_keys(counts) == [
        "repo-a",
        "repo-c",
        "repo-z",
    ]
    assert select_canary_repository_keys({"only": 7}) == ["only"]


def test_promotion_gates_fail_closed_when_external_evidence_is_missing():
    from app.services.gitllery.builds import evaluate_activation_gates

    result = evaluate_activation_gates(
        {
            "integrity_exact": True,
            "duplicate_free": True,
            "resume_verified": True,
            "worker_memory_mb": 100,
            "projected_duration_hours": 2,
            "projected_storage_bytes": 1024,
        },
        require_incremental_soak=False,
    )

    assert result["passed"] is False
    assert "restore_dry_run_verified" in result["failed"]
    assert "api_p95_regression_percent" in result["failed"]


def test_promotion_preserves_legacy_and_recovers_after_interruption(tmp_path, monkeypatch):
    from app.services.gitllery.builds import promote_verified_generation
    from gitllery_format import SegmentRepository

    creator_root = tmp_path / "pixiv" / "creator"
    legacy = creator_root / ".gitllery"
    legacy.mkdir(parents=True)
    (legacy / "HEAD").write_text("legacy\n", encoding="utf-8")
    staged = creator_root / ".gitllery.build-next"
    repository = SegmentRepository(staged)
    repository.initialise(repository_id="repo-1", generation="next")

    gates = {
        "passed": True,
        "incremental_soak_hours": 24,
    }
    real_replace = os.replace
    calls = 0

    def interrupt_second_replace(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated interruption")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", interrupt_second_replace)
    with pytest.raises(OSError, match="simulated interruption"):
        promote_verified_generation(
            creator_root,
            generation="next",
            gates=gates,
        )

    monkeypatch.setattr(os, "replace", real_replace)
    result = promote_verified_generation(
        creator_root,
        generation="next",
        gates=gates,
    )

    assert result["promoted"] is True
    assert (creator_root / ".gitllery.legacy-v0" / "HEAD").read_text() == "legacy\n"
    assert SegmentRepository(creator_root / ".gitllery").verify(deep=True).ok


def test_promotion_refuses_unverified_generation(tmp_path):
    from app.services.gitllery.builds import GitlleryPromotionError
    from app.services.gitllery.builds import promote_verified_generation

    with pytest.raises(GitlleryPromotionError, match="verification gates"):
        promote_verified_generation(
            tmp_path,
            generation="next",
            gates={"passed": False},
        )


def test_active_writer_refuses_an_unpromoted_repository(tmp_path, monkeypatch):
    from app.services.gitllery import builds as build_module
    from app.services.gitllery.builds import (
        GitlleryPromotionError,
        assert_active_repository_ready,
    )
    from gitllery_format import SegmentRepository

    monkeypatch.setattr(
        build_module.settings,
        "gitllery_active_verified_generation",
        "verified-r1",
    )
    with pytest.raises(GitlleryPromotionError, match="not been atomically promoted"):
        assert_active_repository_ready(SegmentRepository(tmp_path / ".gitllery"))


async def _clear_build_fixture(db):
    from sqlalchemy import text

    await db.execute(
        text(
            "TRUNCATE gitllery_builds, gitllery_projection_targets, "
            "gitllery_projection_outbox, gitllery_repository_state, "
            "curation_changes, curation_commits, work_sources, works, "
            "source_creators, creators RESTART IDENTITY CASCADE"
        )
    )
    await db.commit()


async def _seed_repository_history(db, *, commits: int):
    from app.models import (
        Creator,
        CurationChange,
        CurationCommit,
        GitlleryProjectionOutbox,
        SourceCreator,
        Work,
        WorkSource,
    )

    creator = Creator(name="build creator")
    db.add(creator)
    await db.flush()
    db.add(
        SourceCreator(
            creator_id=creator.id,
            source="pixiv",
            source_creator_id="build-creator",
            display_name="build creator",
        )
    )
    work = Work(title="build work")
    db.add(work)
    await db.flush()
    db.add(
        WorkSource(
            work_id=work.id,
            source="pixiv",
            source_work_id="build-work",
            source_creator_id="build-creator",
            raw_metadata={"user": {"id": "build creator"}},
        )
    )
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(commits):
        commit = CurationCommit(
            actor_type="system",
            message=f"commit {index}",
            trigger="test",
            occurred_at=base + timedelta(seconds=index),
            created_at=base + timedelta(seconds=index),
        )
        db.add(commit)
        await db.flush()
        db.add(
            CurationChange(
                commit_id=commit.id,
                subject_type="work",
                subject_id=str(work.id),
                action="work_updated",
                sequence=0,
                before_state={"index": index - 1},
                after_state={"index": index},
                diff={"index": index},
                impact={},
                created_at=base + timedelta(seconds=index),
            )
        )
        db.add(
            GitlleryProjectionOutbox(
                commit_id=commit.id,
                state="pending",
                available_at=base,
            )
        )
        rows.append(commit)
    await db.commit()
    return rows


@pytest.mark.integration
@pytest.mark.asyncio
async def test_build_checkpoints_resume_without_duplicates_and_verifies_exactly(tmp_path, monkeypatch):
    from app.database import async_session, engine
    from app.services.gitllery import builds as build_module
    from app.services.gitllery.builds import GitlleryBuildService
    from gitllery_format import SegmentRepository

    monkeypatch.setattr(build_module.settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear_build_fixture(db)
            await _seed_repository_history(db, commits=205)
            service = GitlleryBuildService(db)
            build = await service.create(scope="canary", generation="resume-r1")

            interrupted = await service.run(
                build.id,
                batch_size=100,
                stop_after_batches=1,
            )
            assert interrupted.state == "running"
            assert interrupted.cursor_commit_id is not None

            # A fresh service instance models a worker restart.  Stop again
            # immediately after the first filesystem batch is published.
            partially_built = await GitlleryBuildService(db).run(
                build.id,
                batch_size=100,
                stop_after_batches=4,
            )
            assert partially_built.state == "running"
            repository = SegmentRepository(tmp_path / "pixiv" / "build creator" / ".gitllery.build-resume-r1")
            assert repository.read_manifest()["commit_count"] == 100

            # Model the narrow crash window after manifest publication but
            # before cursor commit.  Replaying the same batch must be a no-op.
            partially_built.cursor_created_at = None
            partially_built.cursor_commit_id = None
            await db.commit()
            staged = await GitlleryBuildService(db).run(build.id, batch_size=100)
            assert staged.state == "staged"
            assert staged.stats["resume_count"] >= 1
            assert staged.stats["selected_repositories"] == ["pixiv:build-creator"]

            manifest = repository.read_manifest()
            assert manifest["commit_count"] == 205
            assert manifest["change_count"] == 205
            assert manifest["segment_count"] == 3

            # Retrying a staged build is a strict no-op.
            await GitlleryBuildService(db).run(build.id)
            assert repository.read_manifest() == manifest

            verifier = GitlleryBuildService(db)
            verification = await verifier.create_verification(
                repository_id=None,
                build_id=build.id,
                deep=True,
                evidence={
                    "restore_dry_run_verified": True,
                    "api_p95_regression_percent": 5,
                },
            )
            result = await verifier.run_verification(verification.id)
            assert result["ok"] is True
            assert result["gates"]["passed"] is True
            assert result["repositories"]["pixiv:build-creator"]["exact"] is True
            assert (await verifier.get(verification.id)).state == "complete"
            assert await verifier.run_verification(verification.id) == result
    finally:
        async with async_session() as db:
            await _clear_build_fixture(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_build_is_blocked_until_canary_gates_pass(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from app.database import async_session, engine
    from app.services.gitllery import builds as build_module
    from app.services.gitllery.builds import GitlleryBuildService

    monkeypatch.setattr(build_module.settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear_build_fixture(db)
            await _seed_repository_history(db, commits=1)
            service = GitlleryBuildService(db)
            canary = await service.create(scope="canary", generation="gated-r1")
            await service.run(canary.id)
            result = await service.verify(canary.id)
            assert result["ok"] is True
            assert result["gates"]["passed"] is False

            with pytest.raises(HTTPException) as error:
                await service.create(scope="full", generation="must-not-start")
            assert error.value.status_code == 409
            assert error.value.detail["code"] == "gitllery_canary_required"
            assert not list(tmp_path.rglob(".gitllery.build-must-not-start"))
    finally:
        async with async_session() as db:
            await _clear_build_fixture(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_build_settles_only_intents_at_or_below_high_water(tmp_path, monkeypatch):
    from sqlalchemy import select

    from app.database import async_session, engine
    from app.models import (
        CurationChange,
        CurationCommit,
        GitlleryProjectionOutbox,
        Work,
    )
    from app.services.gitllery import builds as build_module
    from app.services.gitllery.builds import GitlleryBuildService

    monkeypatch.setattr(build_module.settings, "library_root", str(tmp_path))
    evidence = {
        "resume_verified": True,
        "restore_dry_run_verified": True,
        "api_p95_regression_percent": 0,
    }
    try:
        async with async_session() as db:
            await _clear_build_fixture(db)
            initial = await _seed_repository_history(db, commits=3)
            service = GitlleryBuildService(db)
            canary = await service.create(scope="canary", generation="canary-r1")
            await service.run(canary.id)
            canary_result = await service.verify(canary.id, evidence=evidence)
            assert canary_result["gates"]["passed"] is True

            full = await service.create(scope="full", generation="full-r1")
            work = await db.scalar(select(Work).limit(1))
            newest = CurationCommit(
                actor_type="system",
                message="after watermark",
                trigger="test",
                occurred_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            )
            db.add(newest)
            await db.flush()
            db.add_all(
                [
                    CurationChange(
                        commit_id=newest.id,
                        subject_type="work",
                        subject_id=str(work.id),
                        action="work_updated",
                        sequence=0,
                        before_state={"index": 2},
                        after_state={"index": 3},
                        diff={"index": 3},
                        impact={},
                    ),
                    GitlleryProjectionOutbox(
                        commit_id=newest.id,
                        state="pending",
                        available_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                    ),
                ]
            )
            await db.commit()
            await service.run(full.id)
            full_result = await service.verify(full.id, evidence=evidence)
            assert full_result["gates"]["passed"] is True

            states = dict(
                (
                    await db.execute(
                        select(
                            GitlleryProjectionOutbox.commit_id,
                            GitlleryProjectionOutbox.state,
                        )
                    )
                ).all()
            )
            assert all(states[commit.id] == "complete" for commit in initial)
            assert states[newest.id] == "pending"
    finally:
        async with async_session() as db:
            await _clear_build_fixture(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_verification_keeps_shadow_generation_and_reports_reason(tmp_path, monkeypatch):
    from app.database import async_session, engine
    from app.services.gitllery import builds as build_module
    from app.services.gitllery.builds import GitlleryBuildService
    from gitllery_format import SegmentRepository

    monkeypatch.setattr(build_module.settings, "library_root", str(tmp_path))
    monkeypatch.setattr(build_module.settings, "gitllery_projection_mode", "shadow")
    try:
        async with async_session() as db:
            await _clear_build_fixture(db)
            await _seed_repository_history(db, commits=1)
            service = GitlleryBuildService(db)
            build = await service.create(scope="canary", generation="corrupt-r1")
            await service.run(build.id)
            repository = SegmentRepository(tmp_path / "pixiv" / "build creator" / ".gitllery.build-corrupt-r1")
            manifest = repository.read_manifest()
            repository._segment_path(manifest["head_segment"]).write_bytes(b"corrupt")

            verification = await service.create_verification(
                repository_id=None,
                build_id=build.id,
                deep=True,
            )
            result = await service.run_verification(verification.id)

            assert result["ok"] is False
            assert (await service.get(build.id)).state == "failed"
            failed_verification = await service.get(verification.id)
            assert failed_verification.state == "failed"
            assert failed_verification.last_error
            assert repository.root.exists()
            assert not (tmp_path / "pixiv" / "build creator" / ".gitllery").exists()
    finally:
        async with async_session() as db:
            await _clear_build_fixture(db)
        await engine.dispose()
