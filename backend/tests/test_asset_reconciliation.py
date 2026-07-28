import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text

from app.services.asset_reconciliation import (
    REVIEW_MIN_SCORE,
    AssetReconciliation,
    metadata_time_bonus,
    phash_distance,
    structural_similarity,
)
from app.services.asset_dedup_scope import canonical_source


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_phash_distance_is_hamming_distance():
    assert phash_distance("0000000000000000", "0000000000000000") == 0
    assert phash_distance("0000000000000000", "000000000000000f") == 4
    assert phash_distance(None, "0") is None
    assert phash_distance("not-hex", "0") is None


def test_cross_source_aliases_and_metadata_time_bands():
    assert canonical_source("x") == "x"
    assert canonical_source("Twitter") == "x"
    assert metadata_time_bonus(None) == 0
    assert metadata_time_bonus(24) == 4
    assert metadata_time_bonus(24.001) == 2
    assert metadata_time_bonus(24 * 7) == 2
    assert metadata_time_bonus(24 * 7 + 0.001) == 1
    assert metadata_time_bonus(24 * 30) == 1
    assert metadata_time_bonus(24 * 30 + 0.001) == 0


def test_structural_similarity_accepts_resize_and_jpeg_compression(tmp_path):
    from PIL import Image, ImageDraw

    original = tmp_path / "original.png"
    variant = tmp_path / "variant.jpg"
    image = Image.new("RGB", (640, 480), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 50, 580, 420), fill=(20, 80, 180))
    draw.ellipse((180, 100, 460, 380), fill=(230, 170, 20))
    image.save(original)
    image.resize((1280, 960)).save(variant, quality=92)

    assert structural_similarity(original, variant) >= 0.98


def test_dedup_settings_require_review_threshold_below_auto_threshold():
    from app.api.admin.settings import DedupSettings

    with pytest.raises(ValidationError):
        DedupSettings(review_score=96, auto_group_score=95)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_processing_outbox_event_is_reclaimed():
    from app.database import async_session, engine
    from app.jobs.asset_dedup import _claim_outbox_event
    from app.models import AssetDedupOutbox

    try:
        async with async_session() as db:
            await db.execute(text("TRUNCATE asset_dedup_outbox CASCADE"))
            event = AssetDedupOutbox(
                idempotency_key="test:stale-outbox",
                event_type="hardlink",
                payload={
                    "asset_id": "00000000-0000-0000-0000-000000000001",
                    "representative_asset_id": "00000000-0000-0000-0000-000000000002",
                },
                state="processing",
                attempts=1,
                available_at=datetime.now(timezone.utc) - timedelta(hours=1),
                updated_at=datetime.now(timezone.utc) - timedelta(minutes=16),
            )
            db.add(event)
            await db.commit()
            event_id = event.id

        claimed = await _claim_outbox_event()

        assert claimed is not None
        assert claimed["id"] == event_id
        assert claimed["attempts"] == 2
    finally:
        async with async_session() as db:
            await db.execute(text("TRUNCATE asset_dedup_outbox CASCADE"))
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_human_negative_decision_is_versioned_and_not_reopened():
    from app.database import async_session, engine
    from app.models import Asset, AssetDedupCase, AssetDedupEvidence
    from app.services.asset_reconciliation import StaleDedupCase

    async def clear(db):
        await db.execute(
            text(
                """
                TRUNCATE
                    asset_dedup_decisions,
                    asset_dedup_cases,
                    asset_dedup_evidence,
                    curation_changes,
                    curation_commits,
                    assets
                RESTART IDENTITY CASCADE
                """
            )
        )
        await db.commit()

    try:
        async with async_session() as db:
            await clear(db)
            assets = [
                Asset(
                    file_path=f"{name}.png",
                    file_name=f"{name}.png",
                    file_size=10,
                    mime_type="image/png",
                    width=10,
                    height=10,
                    sha256=f"{index:064x}",
                    phash="8000000000000000",
                )
                for index, name in enumerate(("left", "right"), start=1)
            ]
            db.add_all(assets)
            await db.flush()
            left_id, right_id = sorted(
                (assets[0].id, assets[1].id),
                key=lambda value: value.int,
            )
            evidence = AssetDedupEvidence(
                left_asset_id=left_id,
                right_asset_id=right_id,
                algorithm_version="test-v1",
                input_digest="a" * 64,
                sha256_equal=False,
                phash_distance=0,
                ssim_score=1.0,
                aspect_ratio_delta=0,
                visual_score=100,
                metadata_score=0,
                total_score=100,
                hard_gate_passed=True,
                facts={"thresholds": {"quarantine_days": 30}},
            )
            db.add(evidence)
            await db.flush()
            case = AssetDedupCase(
                left_asset_id=left_id,
                right_asset_id=right_id,
                evidence_id=evidence.id,
                status="pending",
                revision=1,
                suggested_representative_asset_id=left_id,
            )
            db.add(case)
            await db.commit()
            case_id = case.id
            asset_ids = [asset.id for asset in assets]

            service = AssetReconciliation(db)
            deferred = await service.decide(
                case_id,
                expected_revision=1,
                action="defer",
                representative_asset_id=None,
                actor_type="admin",
                actor_id="reviewer",
                reason=None,
                idempotency_key="test:defer:one",
            )
            await db.commit()
            assert deferred["revision"] == 2

            with pytest.raises(StaleDedupCase):
                await service.decide(
                    case_id,
                    expected_revision=1,
                    action="merge",
                    representative_asset_id=left_id,
                    actor_type="admin",
                    actor_id="stale-reviewer",
                    reason=None,
                    idempotency_key="test:stale:merge",
                )
            await db.rollback()

            separated = await service.decide(
                case_id,
                expected_revision=2,
                action="separate",
                representative_asset_id=None,
                actor_type="admin",
                actor_id="reviewer",
                reason="Different image",
                idempotency_key="test:separate:one",
            )
            await db.commit()
            assert separated["revision"] == 3

            new_evidence = AssetDedupEvidence(
                left_asset_id=left_id,
                right_asset_id=right_id,
                algorithm_version="test-v2",
                input_digest="b" * 64,
                sha256_equal=False,
                phash_distance=0,
                ssim_score=1.0,
                aspect_ratio_delta=0,
                visual_score=100,
                metadata_score=0,
                total_score=100,
                hard_gate_passed=True,
                facts={"thresholds": {"quarantine_days": 30}},
            )
            db.add(new_evidence)
            await db.flush()
            refreshed_assets = [
                await db.get(Asset, asset_id)
                for asset_id in asset_ids
            ]
            refreshed_case, created = await service._upsert_case(
                refreshed_assets[0],
                refreshed_assets[1],
                new_evidence,
            )
            assert created is False
            assert refreshed_case.status == "separate"
            assert refreshed_case.revision == 4
    finally:
        async with async_session() as db:
            await clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_work_purge_keeps_representative_used_by_visible_group_member():
    from app.database import async_session, engine
    from app.models import (
        Asset,
        AssetSource,
        VisualAssetGroup,
        VisualAssetMember,
        Work,
        WorkCurationState,
        WorkSource,
    )
    from app.services.curation import CurationService

    async def clear(db):
        await db.execute(
            text(
                """
                TRUNCATE
                    visual_asset_members,
                    visual_asset_groups,
                    asset_sources,
                    asset_storage_states,
                    work_curation_states,
                    work_sources,
                    works,
                    assets
                RESTART IDENTITY CASCADE
                """
            )
        )
        await db.commit()

    try:
        async with async_session() as db:
            await clear(db)
            trashed_work = Work(title="trashed", is_nsfw=False)
            visible_work = Work(title="visible", is_nsfw=False)
            db.add_all([trashed_work, visible_work])
            await db.flush()
            sources = [
                WorkSource(
                    work_id=work.id,
                    source="pixiv",
                    source_work_id=f"work-{index}",
                )
                for index, work in enumerate(
                    (trashed_work, visible_work),
                    start=1,
                )
            ]
            db.add_all(sources)
            assets = [
                Asset(
                    file_path=f"asset-{index}.png",
                    file_name=f"asset-{index}.png",
                    file_size=10,
                    mime_type="image/png",
                    width=10,
                    height=10,
                    sha256=f"{index:064x}",
                    phash="8000000000000000",
                )
                for index in (1, 2)
            ]
            db.add_all(assets)
            await db.flush()
            db.add_all(
                [
                    AssetSource(
                        asset_id=assets[index].id,
                        work_source_id=sources[index].id,
                        source="pixiv",
                        source_asset_id=f"asset-{index}",
                        ordinal=0,
                        role="page",
                    )
                    for index in (0, 1)
                ]
            )
            db.add(
                WorkCurationState(
                    work_id=trashed_work.id,
                    visibility="trashed",
                )
            )
            group = VisualAssetGroup(
                representative_asset_id=assets[0].id,
                policy_version="test",
            )
            db.add(group)
            await db.flush()
            db.add_all(
                [
                    VisualAssetMember(
                        group_id=group.id,
                        asset_id=asset.id,
                        quality_score=1,
                    )
                    for asset in assets
                ]
            )
            await db.commit()

            candidates = await CurationService(db)._purge_candidate_assets(
                [trashed_work.id]
            )

            assert candidates == []
    finally:
        async with async_session() as db:
            await clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconciliation_excludes_same_work_and_same_source_even_for_exact_sha(
    tmp_path, monkeypatch
):
    from PIL import Image

    from app.config import settings
    from app.database import async_session, engine
    from app.models import (
        Asset,
        AssetDedupCase,
        AssetDedupEvidence,
        AssetDedupOutbox,
        AssetStorageState,
        AssetSource,
        VisualAssetGroup,
        VisualAssetMember,
        Work,
        WorkSource,
    )

    download_root = tmp_path / "downloads"
    download_root.mkdir()
    paths = []
    for index in range(6):
        path = download_root / f"asset-{index}.png"
        colors = ((30, 90, 180), (180, 60, 30), (40, 170, 80))
        Image.new("RGB", (80, 60), colors[index // 2]).save(path)
        paths.append(path)
    monkeypatch.setattr(settings, "download_root", str(download_root))

    async def clear(db):
        await db.execute(
            text(
                """
                TRUNCATE
                    asset_dedup_decisions,
                    asset_dedup_cases,
                    visual_asset_members,
                    visual_asset_groups,
                    asset_dedup_evidence,
                    asset_dedup_outbox,
                    asset_dedup_scans,
                    asset_sources,
                    asset_storage_states,
                    assets,
                    curation_changes,
                    curation_commits,
                    work_sources,
                    works
                RESTART IDENTITY CASCADE
                """
            )
        )
        await db.commit()

    try:
        async with async_session() as db:
            await clear(db)
            same_work = Work(title="intentional difference pages", is_nsfw=False)
            pixiv_works = [
                Work(title="same source left", is_nsfw=False),
                Work(title="same source right", is_nsfw=False),
            ]
            alias_works = [
                Work(title="x alias left", is_nsfw=False),
                Work(title="x alias right", is_nsfw=False),
            ]
            db.add_all([same_work, *pixiv_works, *alias_works])
            await db.flush()
            work_sources = [
                WorkSource(
                    work_id=same_work.id,
                    source="pixiv",
                    source_work_id="96166990",
                ),
                WorkSource(
                    work_id=pixiv_works[0].id,
                    source="pixiv",
                    source_work_id="pixiv-left",
                ),
                WorkSource(
                    work_id=pixiv_works[1].id,
                    source="pixiv",
                    source_work_id="pixiv-right",
                ),
                WorkSource(
                    work_id=alias_works[0].id,
                    source="x",
                    source_work_id="x-left",
                ),
                WorkSource(
                    work_id=alias_works[1].id,
                    source="twitter",
                    source_work_id="twitter-right",
                ),
            ]
            db.add_all(work_sources)
            await db.flush()

            phashes = (
                "1111111111111111",
                "aaaaaaaaaaaaaaaa",
                "5555555555555555",
            )
            assets = [
                Asset(
                    file_path=path.name,
                    file_name=path.name,
                    file_size=path.stat().st_size,
                    mime_type="image/png",
                    width=80,
                    height=60,
                    sha256=_sha(path),
                    phash=phashes[index // 2],
                )
                for index, path in enumerate(paths)
            ]
            db.add_all(assets)
            await db.flush()
            bindings = (
                (0, 0, "pixiv", 0),
                (1, 0, "pixiv", 1),
                (2, 1, "pixiv", 0),
                (3, 2, "pixiv", 0),
                (4, 3, "x", 0),
                (5, 4, "twitter", 0),
            )
            db.add_all(
                [
                    AssetSource(
                        asset_id=assets[asset_index].id,
                        work_source_id=work_sources[source_index].id,
                        source=source,
                        source_asset_id=f"scope-{asset_index}",
                        ordinal=ordinal,
                        role="page",
                    )
                    for asset_index, source_index, source, ordinal in bindings
                ]
            )
            await db.commit()

            service = AssetReconciliation(db)
            same_work_scope = await service.scope.pair(
                assets[0].id, assets[1].id
            )
            same_source_scope = await service.scope.pair(
                assets[2].id, assets[3].id
            )
            alias_scope = await service.scope.pair(
                assets[4].id, assets[5].id
            )
            assert same_work_scope.reason == "same_work"
            assert same_source_scope.reason == "same_source"
            assert alias_scope.reason == "same_source"
            group_scope = await service.scope.group(
                (assets[2].id, assets[3].id, assets[4].id)
            )
            assert group_scope.reason == "group_source_conflict"

            result = await service.observe(
                [assets[0].id, assets[2].id, assets[4].id],
                auto_apply=True,
            )
            await db.commit()
            assert result["candidates_evaluated"] == 0
            assert (
                await db.execute(select(func.count(AssetDedupEvidence.id)))
            ).scalar_one() == 0
            assert (
                await db.execute(select(func.count(AssetDedupCase.id)))
            ).scalar_one() == 0
            assert (
                await db.execute(select(func.count(VisualAssetGroup.id)))
            ).scalar_one() == 0
            assert (
                await db.execute(select(func.count(AssetDedupOutbox.id)))
            ).scalar_one() == 0

            left_id, right_id = sorted(
                (assets[0].id, assets[1].id),
                key=lambda value: value.int,
            )
            legacy_evidence = AssetDedupEvidence(
                left_asset_id=left_id,
                right_asset_id=right_id,
                algorithm_version="legacy-test",
                input_digest="f" * 64,
                sha256_equal=True,
                phash_distance=0,
                ssim_score=1,
                aspect_ratio_delta=0,
                visual_score=100,
                metadata_score=0,
                total_score=100,
                hard_gate_passed=True,
                facts={},
            )
            db.add(legacy_evidence)
            await db.flush()
            legacy_evidence_id = legacy_evidence.id
            legacy_case = AssetDedupCase(
                left_asset_id=left_id,
                right_asset_id=right_id,
                evidence_id=legacy_evidence.id,
                status="pending",
                revision=1,
                suggested_representative_asset_id=left_id,
            )
            db.add(legacy_case)
            await db.commit()

            with pytest.raises(
                ValueError, match="same Work never enter reconciliation"
            ):
                await service.decide(
                    legacy_case.id,
                    expected_revision=1,
                    action="merge",
                    representative_asset_id=left_id,
                    actor_type="admin",
                    actor_id="scope-test",
                    reason=None,
                    idempotency_key="scope-test:same-work",
                )
            await db.rollback()
            assert (
                await db.execute(select(func.count(VisualAssetGroup.id)))
            ).scalar_one() == 0
            assert (
                await db.execute(select(func.count(AssetDedupOutbox.id)))
            ).scalar_one() == 0

            invalid_group = VisualAssetGroup(
                representative_asset_id=left_id,
                policy_version="legacy-test",
            )
            db.add(invalid_group)
            await db.flush()
            invalid_group_id = invalid_group.id
            db.add_all(
                [
                        VisualAssetMember(
                            group_id=invalid_group.id,
                            asset_id=asset_id,
                            evidence_id=legacy_evidence_id,
                        quality_score=1,
                    )
                    for asset_id in (left_id, right_id)
                ]
            )
            db.add(
                AssetStorageState(
                    asset_id=right_id,
                    storage_state="available",
                    served_by_asset_id=left_id,
                    bytes_reclaimed=0,
                )
            )
            await db.commit()

            from app.jobs.asset_dedup import _load_storage_subjects

            with pytest.raises(
                ValueError, match="at most one asset from each Work"
            ):
                await _load_storage_subjects(
                    {
                        "group_id": str(invalid_group_id),
                        "asset_id": str(right_id),
                        "representative_asset_id": str(left_id),
                    }
                )
    finally:
        async with async_session() as db:
            await clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_asset_reconciliation_groups_assets_without_merging_works(
    tmp_path, monkeypatch
):
    from PIL import Image

    from app.config import settings
    from app.database import async_session, engine
    from app.models import (
        Asset,
        AssetDedupOutbox,
        AssetStorageState,
        AssetSource,
        Creator,
        SourceCreator,
        VisualAssetGroup,
        VisualAssetMember,
        Work,
        WorkSource,
    )

    download_root = tmp_path / "downloads"
    download_root.mkdir()
    left_path = download_root / "pixiv.png"
    right_path = download_root / "x.png"
    image = Image.new("RGB", (320, 240), (60, 120, 220))
    image.save(left_path)
    image.save(right_path)
    monkeypatch.setattr(settings, "download_root", str(download_root))

    async def clear(db):
        await db.execute(
            text(
                """
                TRUNCATE
                    asset_dedup_decisions,
                    asset_dedup_cases,
                    visual_asset_members,
                    visual_asset_groups,
                    asset_dedup_evidence,
                    asset_dedup_outbox,
                    asset_dedup_scans,
                    asset_sources,
                    asset_storage_states,
                    assets,
                    curation_changes,
                    curation_commits,
                    work_sources,
                    works,
                    source_creators,
                    creators
                RESTART IDENTITY CASCADE
                """
            )
        )
        await db.commit()

    try:
        async with async_session() as db:
            await clear(db)
            creator = Creator(name="asset-dedup-fixture")
            db.add(creator)
            await db.flush()
            pixiv_creator = SourceCreator(
                creator_id=creator.id,
                source="pixiv",
                source_creator_id="creator-pixiv",
            )
            x_creator = SourceCreator(
                creator_id=creator.id,
                source="x",
                source_creator_id="creator-x",
            )
            db.add_all([pixiv_creator, x_creator])
            await db.flush()

            left_work = Work(title="Pixiv publication", is_nsfw=False)
            right_work = Work(title="X publication", is_nsfw=False)
            db.add_all([left_work, right_work])
            await db.flush()
            now = datetime.now(timezone.utc)
            left_source = WorkSource(
                work_id=left_work.id,
                source="pixiv",
                source_work_id="work-pixiv",
                source_creator_id=pixiv_creator.source_creator_id,
                posted_at=now,
            )
            right_source = WorkSource(
                work_id=right_work.id,
                source="x",
                source_work_id="work-x",
                source_creator_id=x_creator.source_creator_id,
                posted_at=now + timedelta(hours=2),
            )
            db.add_all([left_source, right_source])
            await db.flush()

            digest = _sha(left_path)
            left_asset = Asset(
                file_path=left_path.name,
                file_name=left_path.name,
                file_size=left_path.stat().st_size,
                mime_type="image/png",
                width=320,
                height=240,
                sha256=digest,
                phash="8000000000000000",
            )
            right_asset = Asset(
                file_path=right_path.name,
                file_name=right_path.name,
                file_size=right_path.stat().st_size,
                mime_type="image/png",
                width=320,
                height=240,
                sha256=digest,
                phash="8000000000000000",
            )
            db.add_all([left_asset, right_asset])
            await db.flush()
            db.add_all(
                [
                    AssetSource(
                        asset_id=left_asset.id,
                        work_source_id=left_source.id,
                        source="pixiv",
                        source_asset_id="pixiv-image",
                        ordinal=0,
                        role="page",
                    ),
                    AssetSource(
                        asset_id=right_asset.id,
                        work_source_id=right_source.id,
                        source="x",
                        source_asset_id="x-image",
                        ordinal=0,
                        role="page",
                    ),
                ]
            )
            await db.commit()

            result = await AssetReconciliation(db).observe(
                [left_asset.id], auto_apply=True
            )
            await db.commit()

            assert result["assets_grouped"] == 1
            assert result["bytes_reclaimable"] == left_path.stat().st_size
            assert (
                await db.execute(select(func.count(Work.id)))
            ).scalar_one() == 2
            assert (
                await db.execute(select(func.count(WorkSource.id)))
            ).scalar_one() == 2
            assert (
                await db.execute(select(func.count(VisualAssetGroup.id)))
            ).scalar_one() == 1
            assert (
                await db.execute(select(func.count(VisualAssetMember.id)))
            ).scalar_one() == 2
            outbox = (
                await db.execute(select(AssetDedupOutbox))
            ).scalar_one()
            assert outbox.event_type == "hardlink"

            from app.jobs.asset_dedup import process_asset_dedup_outbox

            drain = await process_asset_dedup_outbox(limit=10)
            assert drain == {"processed": 1, "failed": 0}
            assert left_path.stat().st_ino == right_path.stat().st_ino
            db.expire_all()
            storage_state = (
                await db.execute(
                    select(AssetStorageState).where(
                        AssetStorageState.storage_state == "hardlinked"
                    )
                )
            ).scalar_one()
            assert storage_state.storage_state == "hardlinked"
            assert storage_state.bytes_reclaimed == left_path.stat().st_size
    finally:
        async with async_session() as db:
            await clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_metadata_only_adds_score_after_visual_gate(tmp_path, monkeypatch):
    from PIL import Image, ImageDraw
    import imagehash

    from app.config import settings
    from app.database import async_session, engine
    from app.models import (
        Asset,
        AssetDedupOutbox,
        AssetStorageState,
        AssetSource,
        Creator,
        SourceCreator,
        Work,
        WorkSource,
    )

    download_root = tmp_path / "downloads"
    download_root.mkdir()
    left_path = download_root / "left.png"
    right_path = download_root / "right.jpg"
    image = Image.new("RGB", (640, 480), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((50, 60, 590, 430), fill=(30, 90, 190))
    image.save(left_path)
    image.resize((1280, 960)).save(right_path, quality=94)
    monkeypatch.setattr(settings, "download_root", str(download_root))

    async def clear(db):
        await db.execute(
            text(
                """
                TRUNCATE
                    asset_dedup_decisions,
                    asset_dedup_cases,
                    visual_asset_members,
                    visual_asset_groups,
                    asset_dedup_evidence,
                    asset_dedup_outbox,
                    asset_dedup_scans,
                    asset_sources,
                    asset_storage_states,
                    assets,
                    curation_changes,
                    curation_commits,
                    work_sources,
                    works,
                    source_creators,
                    creators
                RESTART IDENTITY CASCADE
                """
            )
        )
        await db.commit()

    try:
        async with async_session() as db:
            await clear(db)
            creator = Creator(name="metadata-score-fixture")
            db.add(creator)
            await db.flush()
            source_creators = [
                SourceCreator(
                    creator_id=creator.id,
                    source=source,
                    source_creator_id=f"creator-{source}",
                )
                for source in ("pixiv", "x")
            ]
            db.add_all(source_creators)
            works = [
                Work(title="left", is_nsfw=False),
                Work(title="right", is_nsfw=False),
            ]
            db.add_all(works)
            await db.flush()
            posted_at = datetime.now(timezone.utc)
            sources = [
                WorkSource(
                    work_id=works[index].id,
                    source=source,
                    source_work_id=f"work-{source}",
                    source_creator_id=f"creator-{source}",
                    posted_at=posted_at + timedelta(hours=index),
                )
                for index, source in enumerate(("pixiv", "x"))
            ]
            db.add_all(sources)
            await db.flush()
            with Image.open(left_path) as left_image:
                left_phash = str(imagehash.phash(left_image))
            with Image.open(right_path) as right_image:
                right_phash = str(imagehash.phash(right_image))
            assets = [
                Asset(
                    file_path=path.name,
                    file_name=path.name,
                    file_size=path.stat().st_size,
                    mime_type=mime,
                    width=width,
                    height=height,
                    sha256=_sha(path),
                    phash=phash,
                )
                for path, mime, width, height, phash in (
                    (left_path, "image/png", 640, 480, left_phash),
                    (right_path, "image/jpeg", 1280, 960, right_phash),
                )
            ]
            db.add_all(assets)
            await db.flush()
            db.add_all(
                [
                    AssetSource(
                        asset_id=assets[index].id,
                        work_source_id=sources[index].id,
                        source=source,
                        source_asset_id=f"asset-{source}",
                        ordinal=0,
                        role="page",
                    )
                    for index, source in enumerate(("pixiv", "x"))
                ]
            )
            await db.commit()

            facts = await AssetReconciliation(db)._evaluate_pair(
                assets[0], assets[1]
            )

            assert facts["hard_gate_passed"] is True
            assert facts["metadata_score"] == 10
            assert facts["total_score"] >= REVIEW_MIN_SCORE

            unrelated_path = download_root / "unrelated.png"
            Image.new("RGB", (640, 480), "black").save(unrelated_path)
            unrelated = Asset(
                file_path=unrelated_path.name,
                file_name=unrelated_path.name,
                file_size=unrelated_path.stat().st_size,
                mime_type="image/png",
                width=640,
                height=480,
                sha256=_sha(unrelated_path),
                phash="ffffffffffffffff",
            )
            db.add(unrelated)
            await db.flush()
            db.add(
                AssetSource(
                    asset_id=unrelated.id,
                    work_source_id=sources[0].id,
                    source="pixiv",
                    source_asset_id="asset-unrelated",
                    ordinal=1,
                    role="page",
                )
            )
            await db.commit()
            rejected = await AssetReconciliation(db)._evaluate_pair(
                assets[0],
                unrelated,
            )
            assert rejected["hard_gate_passed"] is False
            assert rejected["metadata_score"] == 0
            assert rejected["total_score"] == 0

            result = await AssetReconciliation(db).observe(
                [assets[0].id],
                auto_apply=True,
            )
            await db.commit()
            assert result["assets_grouped"] == 1
            assert result["bytes_reclaimable"] == left_path.stat().st_size
            outbox = (
                await db.execute(select(AssetDedupOutbox))
            ).scalar_one()
            assert outbox.event_type == "quarantine"

            from app.jobs.asset_dedup import process_asset_dedup_outbox

            drain = await process_asset_dedup_outbox(limit=10)
            assert drain == {"processed": 1, "failed": 0}
            assert not left_path.exists()
            db.expire_all()
            storage_state = (
                await db.execute(
                    select(AssetStorageState).where(
                        AssetStorageState.storage_state == "quarantined"
                    )
                )
            ).scalar_one()
            assert storage_state.storage_state == "quarantined"
            assert storage_state.purge_after is not None
            retention = storage_state.purge_after - datetime.now(timezone.utc)
            assert timedelta(days=29, hours=23) < retention <= timedelta(days=30)
            assert (
                download_root / storage_state.quarantine_path
            ).exists()
    finally:
        async with async_session() as db:
            await clear(db)
        await engine.dispose()
