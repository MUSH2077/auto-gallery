"""Read-only grouped creator reference aggregation."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text


PREFIX = "creator_reference_test_"


def _vault():
    from app.services.remote_credentials import CredentialVault

    return CredentialVault(base64.urlsafe_b64encode(b"f" * 32).decode())


class Registry:
    def __init__(self, adapter):
        self.adapter = adapter

    def get(self, source):
        assert source == "pixiv"
        return self.adapter


class ProfileAdapter:
    source = "pixiv"

    async def fetch_creator_profile(self, credentials, *, source_creator_id):
        from app.remote_discovery.contract import RemoteCreatorProfile

        assert credentials.materialize()["refresh_token"] == "reference-secret"
        if source_creator_id == "200":
            raise RuntimeError("one identity failed")
        return RemoteCreatorProfile(
            source="pixiv",
            source_creator_id=source_creator_id,
            display_name=f"Remote {source_creator_id}",
            username=f"account_{source_creator_id}",
            profile_url=f"https://www.pixiv.net/users/{source_creator_id}",
            avatar_url=f"https://i.pximg.net/avatar/{source_creator_id}.jpg",
            comment=None,
            work_counts={},
            is_followed=None,
            fetched_at=datetime.now(timezone.utc),
        )


def _danbooru_artist(artist_id):
    assert artist_id == 55
    return {
        "id": 55,
        "name": "danbooru_primary",
        "other_names": ["alias_one", "alias_two"],
        "urls": [
            {"url": "https://www.pixiv.net/users/200"},
            {"normalized_url": "https://www.pixiv.net/en/users/300"},
            {"url": "https://www.pixiv.net/users/100"},
        ],
    }


async def _cleanup(db):
    params = {"prefix": f"{PREFIX}%"}
    for statement in (
        "DELETE FROM remote_accounts WHERE user_id IN (SELECT id FROM users WHERE username LIKE :prefix)",
        "DELETE FROM source_creators WHERE creator_id IN (SELECT id FROM creators WHERE name LIKE :prefix)",
        "DELETE FROM creators WHERE name LIKE :prefix",
        "DELETE FROM users WHERE username LIKE :prefix",
    ):
        await db.execute(text(statement), params)
    await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_references_merge_all_pixiv_ids_tolerate_partial_failure_and_never_write_mappings():
    from app.database import async_session, engine
    from app.models import Creator, RemoteAccount, SourceCreator, User
    from app.services.creator_references import CreatorReferenceService
    from app.services.remote_accounts import RemoteAccountService

    adapter = ProfileAdapter()
    registry = Registry(adapter)
    try:
        async with async_session() as db:
            await _cleanup(db)
            user = User(
                username=f"{PREFIX}{uuid4().hex[:8]}",
                password_hash="test-only",
                is_active=True,
                permissions=["library", "curation", "subscriptions"],
            )
            creator = Creator(
                name=f"{PREFIX}creator",
                display_name="Local Display",
                danbooru_artist_id=55,
            )
            db.add_all([user, creator])
            await db.flush()
            db.add_all(
                [
                    SourceCreator(
                        creator_id=creator.id,
                        source="pixiv",
                        source_creator_id="100",
                        display_name="Stored 100",
                        raw_metadata={"username": "stored_100"},
                    ),
                    SourceCreator(
                        creator_id=creator.id,
                        source="pixiv",
                        source_creator_id="200",
                        display_name="Stored 200",
                        raw_metadata={"username": "stored_200"},
                    ),
                ]
            )
            account_read = await RemoteAccountService(
                db, user.id, vault=_vault(), adapters=registry
            ).create(
                {
                    "source": "pixiv",
                    "auth_method": "refresh_token",
                    "credentials": {"refresh_token": "reference-secret"},
                }
            )
            account = await db.get(RemoteAccount, account_read.id)
            account.auth_status = "healthy"
            await db.commit()
            before = (
                await db.execute(select(func.count()).select_from(SourceCreator))
            ).scalar_one()

            references = await CreatorReferenceService(
                db,
                user.id,
                vault=_vault(),
                adapters=registry,
                danbooru_lookup=_danbooru_artist,
            ).get_references(creator.id)

            assert [item.source_creator_id for item in references.pixiv] == ["100", "200", "300"]
            assert references.pixiv[0].display_name == "Remote 100"
            assert references.pixiv[0].username == "account_100"
            assert references.pixiv[0].avatar_url.startswith("/api/v1/remote-media/")
            assert references.pixiv[1].display_name == "Stored 200"
            assert references.pixiv[1].username == "stored_200"
            assert references.pixiv[1].status == "fallback"
            assert references.pixiv[2].display_name == "Remote 300"
            assert references.danbooru.name == "danbooru_primary"
            assert references.danbooru.other_names == ["alias_one", "alias_two"]

            after = (
                await db.execute(select(func.count()).select_from(SourceCreator))
            ).scalar_one()
            assert after == before
            assert not db.new and not db.dirty and not db.deleted

            account.is_enabled = False
            await db.flush()
            fallback = await CreatorReferenceService(
                db,
                user.id,
                vault=_vault(),
                adapters=registry,
                danbooru_lookup=_danbooru_artist,
            ).get_references(creator.id)
            assert [item.status for item in fallback.pixiv] == [
                "fallback",
                "fallback",
                "fallback",
            ]
            assert fallback.pixiv[0].display_name == "Stored 100"
            assert fallback.pixiv[2].display_name == "300"
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()
