"""Private candidate detail and work pagination service contract."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text


PREFIX = "remote_creator_access_test_"


def _vault():
    from app.services.remote_credentials import CredentialVault

    return CredentialVault(base64.urlsafe_b64encode(b"r" * 32).decode())


class Registry:
    def __init__(self, adapter):
        self.adapter = adapter

    def get(self, source):
        assert source == "pixiv"
        return self.adapter


class DetailAdapter:
    source = "pixiv"
    auth_methods = ("refresh_token",)

    def __init__(self):
        self.calls = []

    async def fetch_creator_detail(
        self, credentials, *, source_creator_id, work_type="illust", page_size=20
    ):
        from app.remote_discovery.contract import RemoteCreatorDetail

        assert credentials.materialize()["refresh_token"] == "detail-secret"
        self.calls.append(("detail", work_type, None))
        return RemoteCreatorDetail(
            profile=self._profile(source_creator_id),
            works=self._page(source_creator_id, cursor=None, work_type=work_type),
        )

    async def fetch_creator_works(
        self,
        credentials,
        *,
        source_creator_id,
        work_type="illust",
        cursor=None,
        page_size=20,
    ):
        assert credentials.materialize()["refresh_token"] == "detail-secret"
        self.calls.append(("works", work_type, dict(cursor or {})))
        return self._page(source_creator_id, cursor=cursor, work_type=work_type)

    @staticmethod
    def _profile(source_creator_id):
        from app.remote_discovery.contract import (
            RemoteCreatorLink,
            RemoteCreatorProfile,
            RemoteCreatorPublicProfile,
        )

        return RemoteCreatorProfile(
            source="pixiv",
            source_creator_id=source_creator_id,
            display_name="Detail Artist",
            username="detail_artist",
            profile_url=f"https://www.pixiv.net/users/{source_creator_id}",
            avatar_url="https://i.pximg.net/user-profile/example.jpg",
            header_image_url="https://i.pximg.net/user-profile/header.jpg",
            comment="profile",
            work_counts={"illusts": 2, "manga": 1},
            social_counts={"following": 25, "mypixiv": 2, "public_bookmarks": 40},
            public_profile=RemoteCreatorPublicProfile(
                gender="female",
                region="Tokyo",
                birth_day="08-30",
                birth_year=2000,
                job="Illustrator",
            ),
            links=(
                RemoteCreatorLink(kind="website", url="https://artist.example"),
                RemoteCreatorLink(kind="x", url="https://x.com/detail_artist"),
            ),
            is_followed=True,
            fetched_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _page(source_creator_id, cursor, work_type="illust"):
        from app.remote_discovery.contract import RemoteWorkPage, RemoteWorkPreview

        second = bool(cursor)
        work_id = "9902" if second else "9901"
        return RemoteWorkPage(
            items=[
                RemoteWorkPreview(
                    source="pixiv",
                    source_work_id=work_id,
                    source_creator_id=source_creator_id,
                    title=f"Work {work_id}",
                    work_url=f"https://www.pixiv.net/artworks/{work_id}",
                    created_at=datetime.now(timezone.utc),
                    work_type=work_type,
                    page_count=2,
                    x_restrict=1 if second else 0,
                    thumbnail_url=f"https://i.pximg.net/thumb/{work_id}.jpg",
                    preview_urls=[
                        f"https://i.pximg.net/preview/{work_id}_p0.jpg",
                        f"https://i.pximg.net/preview/{work_id}_p1.jpg",
                    ],
                )
            ],
            done=second,
            next_cursor=None if second else {"offset": 1, "work_type": work_type},
        )


def test_optional_profile_media_is_independently_downgraded():
    from app.models import DiscoveryCandidate, RemoteAccount
    from app.remote_discovery.contract import RemoteCreatorProfile
    from app.services.remote_access_tokens import RemoteAccessTokenService
    from app.services.remote_creator_access import RemoteCreatorAccessService

    account = RemoteAccount(
        id=uuid4(),
        user_id=41,
        source="pixiv",
        credential_generation=1,
    )
    candidate = DiscoveryCandidate(
        id=uuid4(),
        remote_account_id=account.id,
        user_id=41,
        source_creator_id="4242",
    )
    profile = RemoteCreatorProfile(
        source="pixiv",
        source_creator_id="4242",
        display_name="Default Avatar Artist",
        username="default_avatar",
        profile_url="https://www.pixiv.net/users/4242",
        avatar_url="https://s.pximg.net/common/images/no_profile.png",
        header_image_url="https://evil.example/header.jpg",
        comment=None,
        work_counts={},
        is_followed=None,
        fetched_at=datetime.now(timezone.utc),
    )
    service = RemoteCreatorAccessService(
        object(),
        41,
        tokens=RemoteAccessTokenService(secret="profile-media-test-secret"),
    )

    read = service._profile_read(candidate, account, profile)

    assert read.avatar_url is None
    assert read.header_image_url is None


async def _cleanup(db):
    params = {"prefix": f"{PREFIX}%"}
    for statement in (
        "DELETE FROM task_events WHERE task_run_id IN (SELECT id FROM task_runs WHERE owner_user_id IN (SELECT id FROM users WHERE username LIKE :prefix))",
        "DELETE FROM task_runs WHERE owner_user_id IN (SELECT id FROM users WHERE username LIKE :prefix)",
        "DELETE FROM download_jobs WHERE owner_user_id IN (SELECT id FROM users WHERE username LIKE :prefix)",
        "DELETE FROM discovery_candidates WHERE user_id IN (SELECT id FROM users WHERE username LIKE :prefix)",
        "DELETE FROM user_subscription_sources WHERE user_id IN (SELECT id FROM users WHERE username LIKE :prefix)",
        "DELETE FROM remote_accounts WHERE user_id IN (SELECT id FROM users WHERE username LIKE :prefix)",
        "DELETE FROM user_subscriptions WHERE user_id IN (SELECT id FROM users WHERE username LIKE :prefix)",
        "DELETE FROM subscription_sources WHERE subscription_id IN (SELECT s.id FROM subscriptions s JOIN creators c ON c.id=s.creator_id WHERE c.name LIKE :prefix)",
        "DELETE FROM subscriptions WHERE creator_id IN (SELECT id FROM creators WHERE name LIKE :prefix)",
        "DELETE FROM source_creators WHERE creator_id IN (SELECT id FROM creators WHERE name LIKE :prefix)",
        "DELETE FROM creators WHERE name LIKE :prefix",
        "DELETE FROM users WHERE username LIKE :prefix",
    ):
        await db.execute(text(statement), params)
    await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_candidate_detail_signs_media_work_and_opaque_cursor_and_enforces_owner():
    from app.database import async_session, engine
    from app.models import DiscoveryCandidate, RemoteAccount, User
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_creator_access import RemoteCreatorAccessService

    adapter = DetailAdapter()
    registry = Registry(adapter)
    try:
        async with async_session() as db:
            await _cleanup(db)
            user = User(
                username=f"{PREFIX}{uuid4().hex[:8]}",
                password_hash="test-only",
                is_active=True,
                permissions=["subscriptions", "tasks"],
            )
            other = User(
                username=f"{PREFIX}{uuid4().hex[:8]}",
                password_hash="test-only",
                is_active=True,
                permissions=["subscriptions"],
            )
            db.add_all([user, other])
            await db.flush()
            account_read = await RemoteAccountService(
                db, user.id, vault=_vault(), adapters=registry
            ).create(
                {
                    "source": "pixiv",
                    "auth_method": "refresh_token",
                    "credentials": {"refresh_token": "detail-secret"},
                }
            )
            account = await db.get(RemoteAccount, account_read.id)
            account.auth_status = "healthy"
            candidate = DiscoveryCandidate(
                remote_account_id=account.id,
                user_id=user.id,
                source_creator_id="4242",
                remote_url="https://www.pixiv.net/users/4242",
                display_name="Snapshot Artist",
                state="pending",
                confidence="high",
                is_following=True,
                candidate_metadata={
                    "profile_image_urls": {
                        "medium": "https://i.pximg.net/user-profile/snapshot.jpg"
                    },
                    "recent_works": [
                        {
                            "source_work_id": "9801",
                            "title": "Snapshot",
                            "work_url": "https://www.pixiv.net/artworks/9801",
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "work_type": "illust",
                            "page_count": 1,
                            "x_restrict": 0,
                            "thumbnail_url": "https://i.pximg.net/thumb/9801.jpg",
                        }
                    ],
                },
            )
            db.add(candidate)
            await db.commit()

            service = RemoteCreatorAccessService(
                db, user.id, vault=_vault(), adapters=registry
            )
            candidate_read = (await service.present_candidates([candidate]))[0]
            assert candidate_read.avatar_url.startswith("/api/v1/remote-media/")
            assert len(candidate_read.recent_works) == 1
            assert candidate_read.recent_works[0].thumbnail_url.startswith(
                "/api/v1/remote-media/"
            )
            detail = await service.get_detail(
                candidate.id,
                work_type="manga",
                limit=20,
            )

            assert detail.candidate.id == candidate.id
            assert detail.profile.display_name == "Detail Artist"
            assert detail.profile.avatar_url.startswith("/api/v1/remote-media/")
            assert detail.profile.header_image_url.startswith("/api/v1/remote-media/")
            assert detail.profile.social_counts == {
                "following": 25,
                "mypixiv": 2,
                "public_bookmarks": 40,
            }
            assert detail.profile.public_profile.region == "Tokyo"
            assert [link.kind for link in detail.profile.links] == ["website", "x"]
            assert len(detail.works.items) == 1
            first = detail.works.items[0]
            assert first.thumbnail_url.startswith("/api/v1/remote-media/")
            assert len(first.preview_urls) == 2
            assert first.work_token
            assert detail.works.next_cursor
            assert "offset" not in detail.works.next_cursor

            page = await service.get_works(
                candidate.id,
                work_type="manga",
                cursor=detail.works.next_cursor,
                limit=20,
            )
            assert [item.source_work_id for item in page.items] == ["9902"]
            assert page.next_cursor is None
            assert adapter.calls == [
                ("detail", "manga", None),
                ("works", "manga", {"offset": 1, "work_type": "manga"}),
            ]

            with pytest.raises(ValueError, match="cursor"):
                await service.get_works(
                    candidate.id,
                    work_type="illust",
                    cursor=detail.works.next_cursor,
                    limit=20,
                )

            with pytest.raises(ValueError, match="not found"):
                await RemoteCreatorAccessService(
                    db, other.id, vault=_vault(), adapters=registry
                ).get_detail(candidate.id, limit=20)
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_single_work_import_creates_manual_member_and_exact_personal_job(monkeypatch):
    from app.database import async_session, engine
    from app.models import DiscoveryCandidate, DownloadJob, RemoteAccount, User, UserSubscription
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_creator_access import RemoteCreatorAccessService
    from app.services.remote_work_import import RemoteWorkImportService
    import app.services.remote_work_import as import_module

    async def no_pressure(*args, **kwargs):
        return None

    async def prepared(*args, **kwargs):
        return object()

    async def published(*args, **kwargs):
        return None

    monkeypatch.setattr(import_module, "download_backpressure_reason", no_pressure)
    monkeypatch.setattr(import_module, "prepare_download_dispatch", prepared)
    monkeypatch.setattr(import_module, "publish_prepared_download", published)

    adapter = DetailAdapter()
    registry = Registry(adapter)
    try:
        async with async_session() as db:
            await _cleanup(db)
            user = User(
                username=f"{PREFIX}{uuid4().hex[:8]}",
                password_hash="test-only",
                is_active=True,
                permissions=["subscriptions", "tasks"],
            )
            db.add(user)
            await db.flush()
            account_read = await RemoteAccountService(
                db, user.id, vault=_vault(), adapters=registry
            ).create(
                {
                    "source": "pixiv",
                    "auth_method": "refresh_token",
                    "credentials": {"refresh_token": "detail-secret"},
                }
            )
            account = await db.get(RemoteAccount, account_read.id)
            account.auth_status = "healthy"
            candidate = DiscoveryCandidate(
                remote_account_id=account.id,
                user_id=user.id,
                source_creator_id="4242",
                remote_url="https://www.pixiv.net/users/4242",
                display_name=f"{PREFIX}artist",
                state="pending",
                confidence="high",
                is_following=True,
            )
            db.add(candidate)
            await db.commit()

            access = RemoteCreatorAccessService(
                db, user.id, vault=_vault(), adapters=registry
            )
            detail = await access.get_detail(candidate.id)
            token = detail.works.items[0].work_token
            result = await RemoteWorkImportService(
                db, user.id, vault=_vault(), adapters=registry
            ).import_work(candidate.id, token, sensitive_content_confirmed=False)
            await db.commit()

            assert result.status == "queued"
            member = await db.get(UserSubscription, candidate.user_subscription_id)
            assert member.schedule_mode == "manual"
            assert member.sync_enabled is False
            job = await db.get(DownloadJob, result.download_job_id)
            assert job.source_url == "https://www.pixiv.net/artworks/9901"
            assert job.triggering_remote_account_id == account.id
            assert job.triggering_credential_generation == account.credential_generation
            assert job.manifest["trigger"] == "remote_work_import"
            assert job.manifest["source_work_id"] == "9901"
            assert job.manifest["candidate_id"] == str(candidate.id)

            duplicate = await RemoteWorkImportService(
                db, user.id, vault=_vault(), adapters=registry
            ).import_work(candidate.id, token, sensitive_content_confirmed=False)
            assert duplicate.status == "already_queued"
            assert duplicate.download_job_id == job.id
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()
