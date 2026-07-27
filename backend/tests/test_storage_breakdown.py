import json
import os

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
            creators
        RESTART IDENTITY CASCADE
    """))
    await db.commit()


def _pixiv_metadata() -> dict:
    return {
        "id": 12345,
        "title": "Pixiv fixture",
        "date": "2026-01-01 12:00:00",
        "user": {"id": 101, "name": "Tree Fixture", "account": "tree_fixture"},
        "tags": [{"name": "tree"}],
    }


def _x_metadata(tweet_id: int, user_id: int, name: str) -> dict:
    return {
        "tweet_id": tweet_id,
        "content": "X fixture #tree",
        "date": "2026-01-02 12:00:00",
        "hashtags": ["tree"],
        "user": {"id": user_id, "name": name, "nick": name},
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_storage_breakdown_aggregates_creator_repositories_and_keeps_unlinked(
    tmp_path,
    monkeypatch,
):
    from app.api.admin import settings as settings_api
    from app.config import settings
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.source_creator import SourceCreator
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource

    download_root = tmp_path / "downloads"
    library_root = tmp_path / "library"
    app_config_root = tmp_path / "config"
    library_root.mkdir()
    app_config_root.mkdir()

    pixiv_dir = download_root / "pixiv" / "101" / "12345"
    x_dir = download_root / "twitter" / "tree_fixture" / "2026-01-02"
    orphan_dir = download_root / "twitter" / "orphan_fixture" / "2026-01-03"
    for directory in (pixiv_dir, x_dir, orphan_dir):
        directory.mkdir(parents=True)
    (pixiv_dir / "12345.json").write_text(json.dumps(_pixiv_metadata()), encoding="utf-8")
    (x_dir / "777_1.json").write_text(
        json.dumps(_x_metadata(777, 202, "tree_fixture")),
        encoding="utf-8",
    )
    (orphan_dir / "888_1.json").write_text(
        json.dumps(_x_metadata(888, 303, "orphan_fixture")),
        encoding="utf-8",
    )
    pixiv_media = pixiv_dir / "12345_1.jpg"
    pixiv_media.write_bytes(b"x" * (1024 * 1024))
    os.link(pixiv_media, x_dir / "777_1.jpg")

    monkeypatch.setattr(settings, "download_root", str(download_root))
    monkeypatch.setattr(settings, "library_root", str(library_root))
    monkeypatch.setattr(settings, "app_config_root", str(app_config_root))
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
            await db.commit()

            payload = await settings_api.storage_breakdown(db=db)

            assert payload["sources"]["x"]["work_count"] == 2
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
            physical_source_mb = sum(
                source["size_mb"] for source in payload["sources"].values()
            )
            logical_source_mb = sum(
                source["logical_size_mb"] for source in payload["sources"].values()
            )
            assert logical_source_mb > physical_source_mb
    finally:
        settings_api.invalidate_storage_breakdown_cache()
        async with async_session() as db:
            await _clear_identity_tables(db)
        await engine.dispose()
