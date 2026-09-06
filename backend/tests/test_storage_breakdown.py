import pytest
from sqlalchemy import text


async def _clear_identity_tables(db):
    await db.execute(text("""
        TRUNCATE
            storage_artifacts,
            import_jobs,
            download_jobs,
            work_source_tags,
            work_tags,
            asset_sources,
            assets,
            work_sources,
            works,
            creator_links,
            source_creators,
            subscription_sources,
            subscriptions,
            creators,
            tags
        RESTART IDENTITY CASCADE
    """))
    await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_storage_breakdown_uses_ledger_rows_for_storage_identity_and_backlog():
    from app.api.admin import settings as settings_api
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.download_job import DownloadJob
    from app.models.source_creator import SourceCreator
    from app.models.storage_artifact import StorageArtifact
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource

    settings_api.invalidate_storage_breakdown_cache()

    try:
        async with async_session() as db:
            await _clear_identity_tables(db)
            creator = Creator(name="tree_fixture", display_name="Tree Fixture")
            db.add(creator)
            await db.flush()
            subscription = Subscription(
                creator_id=creator.id,
                name="Tree Fixture",
                sync_enabled=True,
                schedule_mode="interval",
            )
            db.add(subscription)
            await db.flush()
            pixiv_repo = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id="101",
                source_url="https://www.pixiv.net/users/101",
                is_enabled=True,
            )
            x_repo = SubscriptionSource(
                subscription_id=subscription.id,
                source="x",
                source_creator_id=None,
                source_url="https://x.com/tree_fixture",
                is_enabled=True,
            )
            db.add_all([pixiv_repo, x_repo])
            db.add_all([
                SourceCreator(
                    creator_id=creator.id,
                    source="pixiv",
                    source_creator_id="101",
                    source_url="https://www.pixiv.net/users/101",
                    display_name="Tree Fixture",
                ),
                SourceCreator(
                    creator_id=creator.id,
                    source="x",
                    source_creator_id="202",
                    source_url="https://x.com/tree_fixture",
                    display_name="Tree Fixture",
                ),
            ])
            await db.flush()
            x_job = DownloadJob(
                subscription_id=subscription.id,
                subscription_source_id=x_repo.id,
                source="x",
                source_url="https://x.com/tree_fixture",
                status="downloaded",
            )
            db.add(x_job)
            await db.flush()
            db.add_all([
                StorageArtifact(
                    storage_root="downloads",
                    file_path="pixiv/101/12345/12345.json",
                    source="pixiv",
                    creator_dir="101",
                    source_work_id="12345",
                    file_name="12345.json",
                    artifact_type="metadata_json",
                    file_size=256 * 1024,
                    state="done",
                ),
                StorageArtifact(
                    storage_root="downloads",
                    file_path="pixiv/101/12345/12345_1.jpg",
                    source="pixiv",
                    creator_dir="101",
                    source_work_id="12345",
                    file_name="12345_1.jpg",
                    artifact_type="image",
                    file_size=1024 * 1024,
                    state="done",
                ),
                StorageArtifact(
                    storage_root="downloads",
                    file_path="twitter/tree_fixture/777_1.json",
                    source="x",
                    creator_dir="tree_fixture",
                    source_work_id="777",
                    file_name="777_1.json",
                    artifact_type="metadata_json",
                    file_size=512 * 1024,
                    download_job_id=x_job.id,
                    state="new",
                ),
                StorageArtifact(
                    storage_root="downloads",
                    file_path="twitter/orphan_fixture/888_1.json",
                    source="x",
                    creator_dir="orphan_fixture",
                    source_work_id="888",
                    file_name="888_1.json",
                    artifact_type="metadata_json",
                    file_size=256 * 1024,
                    state="new",
                ),
                StorageArtifact(
                    storage_root="library",
                    file_path="twitter/tree_fixture/777_1.json",
                    source="x",
                    creator_dir="tree_fixture",
                    source_work_id="library-only",
                    file_name="777_1.json",
                    artifact_type="metadata_json",
                    file_size=0,
                    state="new",
                ),
                StorageArtifact(
                    storage_root="library",
                    file_path="twitter/orphan_fixture/888_1.json",
                    source="x",
                    creator_dir="orphan_fixture",
                    source_work_id="library-failed",
                    file_name="888_1.json",
                    artifact_type="metadata_json",
                    file_size=128 * 1024,
                    state="failed",
                ),
            ])
            await db.commit()

            payload = await settings_api.storage_breakdown(db=db)

            assert payload["sources"]["x"]["work_count"] == 2
            assert payload["sources"]["pixiv"]["size_mb"] == 1.2
            assert payload["inventory_source"] == "storage_artifacts"
            assert payload["inventory_updated_at"] is not None
            assert payload["pipeline_stats"] == {
                "pending_import_works": 2,
                "orphan_pending_artifacts": 1,
                "failed_artifacts": 0,
            }
            assert len(payload["creator_tree"]) == 1
            parent = payload["creator_tree"][0]
            assert parent["creator_id"] == str(creator.id)
            assert parent["repository_count"] == 2
            children = {child["source"]: child for child in parent["repositories"]}
            assert children["pixiv"]["repository_id"] == str(pixiv_repo.id)
            assert children["x"]["repository_id"] == str(x_repo.id)
            assert children["x"]["disk_source"] == "twitter"
            assert [row["directory_name"] for row in payload["unlinked_repositories"]] == [
                "orphan_fixture",
            ]
            assert payload["layers"]["original_media_store"]["size_mb"] == 2.0
            assert payload["layers"]["library_index"]["size_mb"] == 0.1
            assert set(payload["db_stats"]) == {
                "works", "assets", "creators", "subscriptions", "tags",
            }
    finally:
        settings_api.invalidate_storage_breakdown_cache()
        async with async_session() as db:
            await _clear_identity_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_storage_breakdown_indexes_repositories_once_and_unlinks_ambiguous_identity(monkeypatch):
    """Directory count must not multiply URL normalization or hide ambiguity."""
    from app.api.admin import settings as settings_api
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.storage_artifact import StorageArtifact
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.providers import registry

    settings_api.invalidate_storage_breakdown_cache()
    provider = registry.get("x")
    normalize_calls = 0
    original_normalize = provider.normalize_url

    def counted_normalize(url):
        nonlocal normalize_calls
        normalize_calls += 1
        return original_normalize(url)

    monkeypatch.setattr(provider, "normalize_url", counted_normalize)
    try:
        async with async_session() as db:
            await _clear_identity_tables(db)
            repositories = []
            for index in range(2):
                creator = Creator(name=f"ambiguous-{index}")
                db.add(creator)
                await db.flush()
                subscription = Subscription(creator_id=creator.id, name=f"Ambiguous {index}")
                db.add(subscription)
                await db.flush()
                repository = SubscriptionSource(
                    subscription_id=subscription.id,
                    source="x",
                    source_creator_id="shared-directory",
                    source_url=f"https://x.com/repository_{index}",
                )
                repositories.append(repository)
                db.add(repository)
            for index in range(12):
                directory = "shared-directory" if index == 0 else f"unlinked-{index:02d}"
                db.add(StorageArtifact(
                    storage_root="downloads",
                    file_path=f"twitter/{directory}/{index}.jpg",
                    source="x",
                    creator_dir=directory,
                    source_work_id=str(index),
                    file_name=f"{index}.jpg",
                    artifact_type="image",
                    file_size=1,
                    state="done",
                ))
            await db.commit()

            payload = await settings_api.storage_breakdown(db=db)

            assert normalize_calls == len(repositories)
            unlinked = {
                row["directory_name"]: row
                for row in payload["unlinked_repositories"]
            }
            assert unlinked["shared-directory"]["repository_id"] is None
            assert all(
                child["directory_name"] != "shared-directory"
                for node in payload["creator_tree"]
                for child in node["repositories"]
            )
    finally:
        settings_api.invalidate_storage_breakdown_cache()
        async with async_session() as db:
            await _clear_identity_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_system_info_reports_ledger_sizes_and_constant_time_disk_capacity():
    from app.api.admin import settings as settings_api
    from app.database import async_session, engine
    from app.models.storage_artifact import StorageArtifact

    settings_api._system_info_cache = None
    settings_api._system_info_cache_ts = 0.0
    try:
        async with async_session() as db:
            await _clear_identity_tables(db)
            db.add_all([
                StorageArtifact(
                    storage_root="downloads",
                    file_path="pixiv/ledger/1/image.jpg",
                    source="pixiv",
                    creator_dir="ledger",
                    source_work_id="1",
                    file_name="image.jpg",
                    artifact_type="image",
                    file_size=1024 * 1024,
                    state="done",
                ),
                StorageArtifact(
                    storage_root="library",
                    file_path="pixiv/ledger/1/index.json",
                    source="pixiv",
                    creator_dir="ledger",
                    source_work_id="1",
                    file_name="index.json",
                    artifact_type="metadata_json",
                    file_size=256 * 1024,
                    state="done",
                ),
            ])
            await db.commit()

            payload = await settings_api.system_info(db=db)

            assert payload["downloads_size_mb"] == 1.0
            assert payload["library_size_mb"] == 0.2
            assert payload["inventory_source"] == "storage_artifacts"
            assert payload["inventory_updated_at"] is not None
            assert payload["downloads_free_gb"] >= 0
            assert set(payload["db_stats"]) == {
                "works", "assets", "creators", "subscriptions", "tags",
            }
    finally:
        settings_api._system_info_cache = None
        settings_api._system_info_cache_ts = 0.0
        async with async_session() as db:
            await _clear_identity_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_storage_breakdown_links_provider_url_directory_to_source_creator_without_repository():
    from app.api.admin import settings as settings_api
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.source_creator import SourceCreator
    from app.models.storage_artifact import StorageArtifact

    settings_api.invalidate_storage_breakdown_cache()
    try:
        async with async_session() as db:
            await _clear_identity_tables(db)
            creator = Creator(name="url-owner", display_name="URL Owner")
            db.add(creator)
            await db.flush()
            db.add(SourceCreator(
                creator_id=creator.id,
                source="x",
                source_creator_id="opaque-202",
                source_url="https://x.com/url_owner",
                display_name="URL Owner",
            ))
            db.add(StorageArtifact(
                storage_root="downloads",
                file_path="twitter/url_owner/900_1.jpg",
                source="x",
                creator_dir="url_owner",
                source_work_id="900",
                file_name="900_1.jpg",
                artifact_type="image",
                file_size=1024,
                state="done",
            ))
            await db.commit()

            payload = await settings_api.storage_breakdown(db=db)

            assert payload["unlinked_repositories"] == []
            assert payload["creator_tree"] == [{
                "creator_id": str(creator.id),
                "display_name": "URL Owner",
                "size_mb": 0.0,
                "work_count": 1,
                "repository_count": 1,
                "repositories": [{
                    "repository_id": None,
                    "source": "x",
                    "source_display_name": "X / Twitter",
                    "disk_source": "twitter",
                    "directory_name": "url_owner",
                    "size_mb": 0.0,
                    "logical_size_mb": 0.0,
                    "work_count": 1,
                }],
            }]
    finally:
        settings_api.invalidate_storage_breakdown_cache()
        async with async_session() as db:
            await _clear_identity_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_storage_breakdown_only_assigns_repository_for_exact_source_identity_match():
    from app.api.admin import settings as settings_api
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.source_creator import SourceCreator
    from app.models.storage_artifact import StorageArtifact
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource

    settings_api.invalidate_storage_breakdown_cache()
    try:
        async with async_session() as db:
            await _clear_identity_tables(db)
            creator = Creator(name="multi-repository", display_name="Multi Repository")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="Multi Repository")
            db.add(subscription)
            await db.flush()
            unmatched = SubscriptionSource(
                subscription_id=subscription.id,
                source="x",
                source_url="https://x.com/another_repository",
            )
            matched = SubscriptionSource(
                subscription_id=subscription.id,
                source="x",
                source_url="https://x.com/matching_repository",
            )
            db.add_all([unmatched, matched])
            db.add_all([
                SourceCreator(
                    creator_id=creator.id,
                    source="x",
                    source_creator_id="opaque-101",
                    source_url="https://x.com/matching_repository",
                    display_name="Multi Repository",
                ),
                SourceCreator(
                    creator_id=creator.id,
                    source="x",
                    source_creator_id="opaque-202",
                    source_url="https://x.com/no_repository_match",
                    display_name="Multi Repository",
                ),
            ])
            await db.flush()
            db.add_all([
                StorageArtifact(
                    storage_root="downloads",
                    file_path="twitter/matching_repository/101_1.jpg",
                    source="x",
                    creator_dir="matching_repository",
                    source_work_id="101",
                    file_name="101_1.jpg",
                    artifact_type="image",
                    file_size=1024,
                    state="done",
                ),
                StorageArtifact(
                    storage_root="downloads",
                    file_path="twitter/no_repository_match/202_1.jpg",
                    source="x",
                    creator_dir="no_repository_match",
                    source_work_id="202",
                    file_name="202_1.jpg",
                    artifact_type="image",
                    file_size=1024,
                    state="done",
                ),
            ])
            await db.commit()

            payload = await settings_api.storage_breakdown(db=db)

            assert payload["unlinked_repositories"] == []
            repositories = {
                row["directory_name"]: row
                for row in payload["creator_tree"][0]["repositories"]
            }
            assert repositories["matching_repository"]["repository_id"] == str(matched.id)
            assert repositories["no_repository_match"]["repository_id"] is None
            assert repositories["no_repository_match"]["source"] == "x"
    finally:
        settings_api.invalidate_storage_breakdown_cache()
        async with async_session() as db:
            await _clear_identity_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_storage_breakdown_uses_work_source_identity_for_username_directory():
    """A provider username directory must resolve through its imported works."""
    from app.api.admin import settings as settings_api
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.source_creator import SourceCreator
    from app.models.storage_artifact import StorageArtifact
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.models.work import Work
    from app.models.work_source import WorkSource

    settings_api.invalidate_storage_breakdown_cache()
    try:
        async with async_session() as db:
            await _clear_identity_tables(db)
            creator = Creator(name="numeric-pixiv-owner", display_name="Numeric Pixiv Owner")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="Numeric Pixiv Owner")
            db.add(subscription)
            await db.flush()
            repository = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id="104836911",
                source_url="https://www.pixiv.net/users/104836911",
            )
            work = Work(title="Imported Pixiv work")
            db.add_all([
                repository,
                work,
                SourceCreator(
                    creator_id=creator.id,
                    source="pixiv",
                    source_creator_id="104836911",
                    source_url="https://www.pixiv.net/users/104836911",
                    display_name="Numeric Pixiv Owner",
                ),
            ])
            await db.flush()
            db.add_all([
                WorkSource(
                    work_id=work.id,
                    source="pixiv",
                    source_work_id="9001",
                    source_creator_id="104836911",
                    source_url="https://www.pixiv.net/artworks/9001",
                ),
                StorageArtifact(
                    storage_root="downloads",
                    file_path="pixiv/user_jjem4255/9001/9001_p0.jpg",
                    source="pixiv",
                    creator_dir="user_jjem4255",
                    source_work_id="9001",
                    file_name="9001_p0.jpg",
                    artifact_type="image",
                    file_size=1024,
                    state="done",
                ),
            ])
            await db.commit()

            payload = await settings_api.storage_breakdown(db=db)

            assert payload["unlinked_repositories"] == []
            assert len(payload["creator_tree"]) == 1
            node = payload["creator_tree"][0]
            assert node["creator_id"] == str(creator.id)
            assert node["display_name"] == "Numeric Pixiv Owner"
            assert node["repositories"] == [{
                "repository_id": str(repository.id),
                "source": "pixiv",
                "source_display_name": "Pixiv",
                "disk_source": "pixiv",
                "directory_name": "user_jjem4255",
                "size_mb": 0.0,
                "logical_size_mb": 0.0,
                "work_count": 1,
            }]
    finally:
        settings_api.invalidate_storage_breakdown_cache()
        async with async_session() as db:
            await _clear_identity_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_storage_breakdown_does_not_hide_conflicting_work_identities():
    """A directory containing works from multiple creators must remain unlinked."""
    from app.api.admin import settings as settings_api
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.source_creator import SourceCreator
    from app.models.storage_artifact import StorageArtifact
    from app.models.work import Work
    from app.models.work_source import WorkSource

    settings_api.invalidate_storage_breakdown_cache()
    try:
        async with async_session() as db:
            await _clear_identity_tables(db)
            first_creator = Creator(name="conflicting-work-owner-a")
            second_creator = Creator(name="conflicting-work-owner-b")
            first_work = Work(title="First imported work")
            second_work = Work(title="Second imported work")
            db.add_all([first_creator, second_creator, first_work, second_work])
            await db.flush()
            db.add_all([
                SourceCreator(
                    creator_id=first_creator.id,
                    source="x",
                    source_creator_id="owner-a",
                    source_url="https://x.com/shared_username",
                ),
                SourceCreator(
                    creator_id=second_creator.id,
                    source="x",
                    source_creator_id="owner-b",
                    source_url="https://x.com/owner_b",
                ),
                WorkSource(
                    work_id=first_work.id,
                    source="x",
                    source_work_id="work-a",
                    source_creator_id="owner-a",
                ),
                WorkSource(
                    work_id=second_work.id,
                    source="x",
                    source_work_id="work-b",
                    source_creator_id="owner-b",
                ),
                StorageArtifact(
                    storage_root="downloads",
                    file_path="twitter/shared_username/work-a.jpg",
                    source="x",
                    creator_dir="shared_username",
                    source_work_id="work-a",
                    file_name="work-a.jpg",
                    artifact_type="image",
                    file_size=1,
                    state="done",
                ),
                StorageArtifact(
                    storage_root="downloads",
                    file_path="twitter/shared_username/work-b.jpg",
                    source="x",
                    creator_dir="shared_username",
                    source_work_id="work-b",
                    file_name="work-b.jpg",
                    artifact_type="image",
                    file_size=1,
                    state="done",
                ),
            ])
            await db.commit()

            payload = await settings_api.storage_breakdown(db=db)

            assert payload["creator_tree"] == []
            assert [
                row["directory_name"]
                for row in payload["unlinked_repositories"]
            ] == ["shared_username"]
    finally:
        settings_api.invalidate_storage_breakdown_cache()
        async with async_session() as db:
            await _clear_identity_tables(db)
        await engine.dispose()
