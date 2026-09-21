"""Private subscription membership behavior and public overlay contracts."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text


PREFIX = "remote_membership_test_"


async def _clear(db) -> None:
    params = {"prefix": f"{PREFIX}%"}
    await db.execute(
        text(
            "DELETE FROM task_events WHERE task_run_id IN (SELECT id FROM task_runs WHERE "
            "triggering_user_subscription_id IN (SELECT id FROM user_subscriptions WHERE user_id IN "
            "(SELECT id FROM users WHERE username LIKE :prefix)))"
        ),
        params,
    )
    await db.execute(
        text(
            "DELETE FROM task_runs WHERE triggering_user_subscription_id IN "
            "(SELECT id FROM user_subscriptions WHERE user_id IN "
            "(SELECT id FROM users WHERE username LIKE :prefix))"
        ),
        params,
    )
    await db.execute(
        text(
            "DELETE FROM import_jobs WHERE download_job_id IN "
            "(SELECT id FROM download_jobs WHERE triggering_user_subscription_id IN "
            "(SELECT id FROM user_subscriptions WHERE user_id IN "
            "(SELECT id FROM users WHERE username LIKE :prefix)))"
        ),
        params,
    )
    await db.execute(
        text(
            "DELETE FROM download_jobs WHERE triggering_user_subscription_id IN "
            "(SELECT id FROM user_subscriptions WHERE user_id IN "
            "(SELECT id FROM users WHERE username LIKE :prefix))"
        ),
        params,
    )
    await db.execute(
        text(
            "DELETE FROM discovery_candidates WHERE user_id IN "
            "(SELECT id FROM users WHERE username LIKE :prefix)"
        ),
        params,
    )
    await db.execute(
        text(
            "DELETE FROM user_subscription_sources WHERE user_id IN "
            "(SELECT id FROM users WHERE username LIKE :prefix)"
        ),
        params,
    )
    await db.execute(
        text(
            "DELETE FROM remote_accounts WHERE user_id IN "
            "(SELECT id FROM users WHERE username LIKE :prefix)"
        ),
        params,
    )
    await db.execute(
        text(
            "DELETE FROM user_subscriptions WHERE user_id IN "
            "(SELECT id FROM users WHERE username LIKE :prefix)"
        ),
        params,
    )
    await db.execute(text("DELETE FROM users WHERE username LIKE :prefix"), params)
    await db.commit()


async def _seed_user(db, suffix: str, *, admin: bool = False):
    from app.models import User

    user = User(
        username=f"{PREFIX}{suffix}_{uuid4().hex[:8]}",
        password_hash="test-only",
        is_admin=admin,
        is_active=True,
        permissions=["library", "subscriptions", "tasks"],
    )
    db.add(user)
    await db.flush()
    return user


@pytest.mark.integration
@pytest.mark.asyncio
async def test_membership_service_overlays_and_isolates_shared_subscription():
    """Removing/mutating one membership must not affect shared rows or another user."""
    from app.database import async_session, engine
    from app.models import Creator, Subscription, SubscriptionSource
    from app.services.subscription_membership import SubscriptionMembershipService

    try:
        async with async_session() as db:
            await _clear(db)
            first = await _seed_user(db, "first")
            second = await _seed_user(db, "second")
            creator = Creator(name="Shared Artist")
            db.add(creator)
            await db.flush()
            canonical = Subscription(
                creator_id=creator.id,
                name="Canonical Name",
                is_active=False,
                sync_enabled=False,
                schedule_mode="manual",
            )
            db.add(canonical)
            await db.flush()
            source = SubscriptionSource(
                subscription_id=canonical.id,
                source="pixiv",
                source_creator_id="31415",
                source_url="https://www.pixiv.net/users/31415",
                is_enabled=False,
            )
            db.add(source)
            await db.flush()

            first_service = SubscriptionMembershipService(db, first.id)
            second_service = SubscriptionMembershipService(db, second.id)
            first_member = await first_service.ensure_membership(
                canonical,
                name="First Private Name",
                is_active=True,
                sync_enabled=True,
                sync_interval_hours=12,
            )
            second_member = await second_service.ensure_membership(
                canonical,
                name="Second Private Name",
                is_active=True,
                sync_enabled=True,
                sync_interval_hours=24,
            )
            await first_service.ensure_source_binding(first_member, source, is_enabled=True)
            await second_service.ensure_source_binding(second_member, source, is_enabled=False)
            await db.commit()

            first_view = await first_service.get(canonical.id)
            second_view = await second_service.get(canonical.id)
            assert first_view.id == canonical.id
            assert first_view.membership_id == first_member.id
            assert first_view.name == "First Private Name"
            assert first_view.sync_interval_hours == 12
            assert second_view.membership_id == second_member.id
            assert second_view.name == "Second Private Name"
            assert [item.id for item in await first_service.list()] == [canonical.id]

            await first_service.update(canonical.id, {"name": "First Renamed", "is_active": False})
            await db.commit()
            assert (await second_service.get(canonical.id)).name == "Second Private Name"
            assert (await db.get(Subscription, canonical.id)).name == "Canonical Name"

            first_sources = await first_service.list_sources(canonical.id)
            second_sources = await second_service.list_sources(canonical.id)
            assert first_sources[0].membership_source_id is not None
            assert first_sources[0].is_enabled is True
            assert second_sources[0].is_enabled is False

            await first_service.remove(canonical.id)
            await db.commit()
            assert await db.get(Subscription, canonical.id) is not None
            assert await db.get(SubscriptionSource, source.id) is not None
            assert (await second_service.get(canonical.id)).membership_id == second_member.id
            with pytest.raises(ValueError, match="Subscription not found"):
                await first_service.get(canonical.id)
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recompute_canonical_membership_cache_uses_active_members_and_earliest_due():
    """Canonical compatibility columns must be a single deterministic aggregate cache."""
    from datetime import datetime, timezone

    from app.database import async_session, engine
    from app.models import Creator, Subscription, SubscriptionSource
    from app.services.subscription_membership import (
        SubscriptionMembershipService,
        recompute_subscription_membership_cache,
    )

    early = datetime(2026, 8, 28, 1, tzinfo=timezone.utc)
    late = datetime(2026, 8, 28, 2, tzinfo=timezone.utc)
    try:
        async with async_session() as db:
            await _clear(db)
            first = await _seed_user(db, "aggregate_first")
            second = await _seed_user(db, "aggregate_second")
            creator = Creator(name="Aggregate Artist")
            db.add(creator)
            await db.flush()
            canonical = Subscription(creator_id=creator.id, name="Aggregate")
            db.add(canonical)
            await db.flush()
            source = SubscriptionSource(
                subscription_id=canonical.id,
                source="x",
                source_creator_id="aggregate",
                source_url="https://x.com/aggregate",
            )
            db.add(source)
            await db.flush()
            first_member = await SubscriptionMembershipService(db, first.id).ensure_membership(canonical)
            second_member = await SubscriptionMembershipService(db, second.id).ensure_membership(canonical)
            first_binding = await SubscriptionMembershipService(db, first.id).ensure_source_binding(
                first_member, source, is_enabled=True
            )
            second_binding = await SubscriptionMembershipService(db, second.id).ensure_source_binding(
                second_member, source, is_enabled=True
            )
            first_binding.next_sync_at = late
            second_binding.next_sync_at = early
            second_member.is_active = False
            await recompute_subscription_membership_cache(db, canonical.id)
            await db.commit()

            await db.refresh(canonical)
            await db.refresh(source)
            assert canonical.is_active is True
            assert canonical.sync_enabled is True
            assert source.is_enabled is True
            assert source.next_sync_at == late

            first_member.is_active = False
            await recompute_subscription_membership_cache(db, canonical.id)
            await db.commit()
            await db.refresh(canonical)
            await db.refresh(source)
            assert canonical.is_active is False
            assert canonical.sync_enabled is False
            assert source.is_enabled is False
            assert source.next_sync_at is None
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


def test_public_subscription_and_source_schemas_expose_private_overlay_fields():
    from app.schemas.subscription import SubscriptionRead
    from app.schemas.subscription_source import SubscriptionSourceRead

    assert {"membership_id", "discovery_candidate_id", "discovered_via"} <= set(
        SubscriptionRead.model_fields
    )
    assert {
        "membership_source_id",
        "remote_account_id",
        "auth_healthy",
        "next_sync_at",
    } <= set(SubscriptionSourceRead.model_fields)


def _headers(username: str) -> dict[str, str]:
    from app.auth import create_access_token

    return {
        "Authorization": f"Bearer {create_access_token(username, must_change_password=False)}"
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_public_subscription_routes_use_current_membership_and_canonical_id():
    """A user's list/detail/write/delete surface must not reveal or mutate peers."""
    from httpx import ASGITransport, AsyncClient

    from app.database import async_session, engine
    from app.main import app
    from app.models import Creator, Subscription, SubscriptionSource
    from app.services.subscription_membership import SubscriptionMembershipService

    try:
        async with async_session() as db:
            await _clear(db)
            first = await _seed_user(db, "api_first")
            second = await _seed_user(db, "api_second")
            creator = Creator(name="API Shared Artist")
            db.add(creator)
            await db.flush()
            canonical = Subscription(creator_id=creator.id, name="Canonical API Name")
            db.add(canonical)
            await db.flush()
            source = SubscriptionSource(
                subscription_id=canonical.id,
                source="pixiv",
                source_creator_id="2718",
                source_url="https://www.pixiv.net/users/2718",
            )
            db.add(source)
            await db.flush()
            member = await SubscriptionMembershipService(db, first.id).ensure_membership(
                canonical, name="First API Name"
            )
            await SubscriptionMembershipService(db, first.id).ensure_source_binding(member, source)
            await db.commit()
            canonical_id = canonical.id
            creator_id = creator.id
            first_name = first.username
            second_name = second.username

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first_headers = _headers(first_name)
            second_headers = _headers(second_name)

            response = await client.get("/api/v1/subscriptions", headers=first_headers)
            assert response.status_code == 200, response.text
            assert [(item["id"], item["name"]) for item in response.json()] == [
                (str(canonical_id), "First API Name")
            ]
            assert response.json()[0]["membership_id"]

            response = await client.get(
                f"/api/v1/subscriptions/{canonical_id}", headers=second_headers
            )
            assert response.status_code == 404
            assert (await client.get("/api/v1/subscriptions/count", headers=second_headers)).json() == {
                "count": 0
            }

            response = await client.post(
                "/api/v1/subscriptions",
                json={"creator_id": str(creator_id), "name": "Second API Name"},
                headers=second_headers,
            )
            assert response.status_code == 201, response.text
            assert response.json()["id"] == str(canonical_id)
            assert response.json()["name"] == "Second API Name"

            response = await client.patch(
                f"/api/v1/subscriptions/{canonical_id}",
                json={"name": "First API Renamed", "sync_interval_hours": 18},
                headers=first_headers,
            )
            assert response.status_code == 200, response.text
            assert response.json()["name"] == "First API Renamed"
            second_view = await client.get(
                f"/api/v1/subscriptions/{canonical_id}", headers=second_headers
            )
            assert second_view.json()["name"] == "Second API Name"

            sources = await client.get(
                f"/api/v1/subscriptions/{canonical_id}/sources", headers=first_headers
            )
            assert sources.status_code == 200, sources.text
            assert sources.json()[0]["membership_source_id"]

            response = await client.delete(
                f"/api/v1/subscriptions/{canonical_id}", headers=first_headers
            )
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "soft_deleted"
            assert (
                await client.get(f"/api/v1/subscriptions/{canonical_id}", headers=first_headers)
            ).status_code == 404
            assert (
                await client.get(f"/api/v1/subscriptions/{canonical_id}", headers=second_headers)
            ).status_code == 200
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_existing_source_uses_authoritative_enable_transition():
    """POST and PATCH share disable clearing and schedule-aware re-enable."""

    from datetime import datetime, timedelta, timezone

    from httpx import ASGITransport, AsyncClient

    from app.database import async_session, engine
    from app.main import app
    from app.models import (
        Creator,
        Subscription,
        SubscriptionSource,
        UserSubscriptionSource,
    )
    from app.services.subscription_membership import (
        SubscriptionMembershipService,
        recompute_subscription_membership_cache,
        select_eligible_membership_source,
    )

    now = datetime.now(timezone.utc).replace(microsecond=0)
    expected_interval_due = now + timedelta(hours=4)
    try:
        async with async_session() as db:
            await _clear(db)
            user = await _seed_user(db, "source_transition")
            creator = Creator(name="Source Transition Artist")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="Transition")
            db.add(subscription)
            await db.flush()
            source = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id="source-transition",
                source_url="https://www.pixiv.net/users/77331",
            )
            db.add(source)
            await db.flush()
            service = SubscriptionMembershipService(db, user.id)
            member = await service.ensure_membership(
                subscription,
                sync_enabled=True,
                sync_interval_hours=4,
                schedule_mode="interval",
            )
            binding = await service.ensure_source_binding(
                member,
                source,
                is_enabled=True,
            )
            binding.last_synced_at = now
            binding.next_sync_at = expected_interval_due
            await recompute_subscription_membership_cache(db, subscription.id)
            await db.commit()
            user_name = user.username
            user_id = user.id
            subscription_id = subscription.id
            source_id = source.id
            member_id = member.id
            binding_id = binding.id

        payload = {
            "source": "pixiv",
            "source_creator_id": "source-transition",
            "source_url": "https://www.pixiv.net/users/77331",
        }
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = _headers(user_name)
            unchanged_enabled = await client.post(
                f"/api/v1/subscriptions/{subscription_id}/sources",
                json={**payload, "is_enabled": True},
                headers=headers,
            )
            assert unchanged_enabled.status_code == 201, unchanged_enabled.text
            assert datetime.fromisoformat(
                unchanged_enabled.json()["next_sync_at"]
            ) == expected_interval_due

            post_disabled = await client.post(
                f"/api/v1/subscriptions/{subscription_id}/sources",
                json={**payload, "is_enabled": False},
                headers=headers,
            )
            assert post_disabled.status_code == 201, post_disabled.text
            assert post_disabled.json()["is_enabled"] is False
            assert post_disabled.json()["next_sync_at"] is None

            async with async_session() as db:
                stored_source = await db.get(SubscriptionSource, source_id)
                stored_binding = await db.get(UserSubscriptionSource, binding_id)
                assert stored_binding.next_sync_at is None
                assert stored_source.is_enabled is False
                assert await select_eligible_membership_source(
                    db,
                    stored_source,
                    now=now,
                    preferred_membership_id=member_id,
                    require_due=False,
                    require_sync_enabled=False,
                ) is None

            post_enabled = await client.post(
                f"/api/v1/subscriptions/{subscription_id}/sources",
                json={**payload, "is_enabled": True},
                headers=headers,
            )
            assert post_enabled.status_code == 201, post_enabled.text
            assert datetime.fromisoformat(
                post_enabled.json()["next_sync_at"]
            ) == expected_interval_due

            patched_disabled = await client.patch(
                f"/api/v1/subscriptions/{subscription_id}/sources/{source_id}",
                json={"is_enabled": False},
                headers=headers,
            )
            assert patched_disabled.status_code == 200, patched_disabled.text
            assert patched_disabled.json()["next_sync_at"] is None

            post_reenabled = await client.post(
                f"/api/v1/subscriptions/{subscription_id}/sources",
                json={**payload, "is_enabled": True},
                headers=headers,
            )
            assert post_reenabled.status_code == 201, post_reenabled.text
            assert datetime.fromisoformat(
                post_reenabled.json()["next_sync_at"]
            ) == expected_interval_due

            async with async_session() as db:
                await SubscriptionMembershipService(db, user_id).update(
                    subscription_id,
                    {"schedule_mode": "manual"},
                )
                await db.commit()

            await client.patch(
                f"/api/v1/subscriptions/{subscription_id}/sources/{source_id}",
                json={"is_enabled": False},
                headers=headers,
            )
            manual_enabled = await client.post(
                f"/api/v1/subscriptions/{subscription_id}/sources",
                json={**payload, "is_enabled": True},
                headers=headers,
            )
            assert manual_enabled.status_code == 201, manual_enabled.text
            assert manual_enabled.json()["next_sync_at"] is None

        async with async_session() as db:
            stored_source = await db.get(SubscriptionSource, source_id)
            stored_binding = await db.get(UserSubscriptionSource, binding_id)
            assert stored_binding.is_enabled is True
            assert stored_binding.next_sync_at is None
            assert stored_source.is_enabled is False
            assert await select_eligible_membership_source(
                db,
                stored_source,
                now=now,
                preferred_membership_id=member_id,
                require_due=False,
                require_sync_enabled=True,
            ) is None
            manual_selection = await select_eligible_membership_source(
                db,
                stored_source,
                now=now,
                preferred_membership_id=member_id,
                require_due=False,
                require_sync_enabled=False,
            )
            assert manual_selection is not None
            assert manual_selection.binding.id == binding_id
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subscription_search_intersects_membership_before_pagination(monkeypatch):
    """Global search pages cannot crowd out or disclose the user's private page."""
    from httpx import ASGITransport, AsyncClient

    from app.database import async_session, engine
    from app.main import app
    from app.models import Creator, Subscription
    from app.services.search import SearchService
    from app.services.subscription_membership import SubscriptionMembershipService

    captured = {}
    try:
        async with async_session() as db:
            await _clear(db)
            first = await _seed_user(db, "search_first")
            creator = Creator(name="Search Owned Artist")
            foreign_creator = Creator(name="Search Foreign Artist")
            db.add_all([creator, foreign_creator])
            await db.flush()
            owned = Subscription(creator_id=creator.id, name="Search Owned")
            foreign = Subscription(creator_id=foreign_creator.id, name="Search Foreign")
            db.add_all([owned, foreign])
            await db.flush()
            await SubscriptionMembershipService(db, first.id).ensure_membership(
                owned, name="Private Search Name"
            )
            await db.commit()
            first_name = first.username
            actor_id = first.id
            owned_id = owned.id
            foreign_id = foreign.id

        async def fake_search(
            self,
            query,
            offset=0,
            limit=20,
            *,
            user_id=None,
            allowed_subscription_ids=None,
            **kwargs,
        ):
            captured.update(actor=user_id, offset=offset, limit=limit, ids=allowed_subscription_ids)
            # Include a stale/foreign hit to exercise the endpoint's current
            # membership guard separately from actor routing into search.
            return {
                "groups": {"subscriptions": {"items": [
                    {"id": str(foreign_id)}, {"id": str(owned_id)},
                ]}}
            }

        monkeypatch.setattr(SearchService, "search", fake_search)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/v1/subscriptions",
                params={"q": "Search", "offset": 17, "limit": 1},
                headers=_headers(first_name),
            )
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()] == [str(owned_id)]
        assert captured == {"actor": actor_id, "offset": 17, "limit": 1, "ids": None}
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repository_detail_is_member_filtered_and_overlays_private_source_policy():
    """The canonical repository detail URL must expose only the caller's binding."""
    from httpx import ASGITransport, AsyncClient

    from app.database import async_session, engine
    from app.main import app
    from app.models import Creator, Subscription, SubscriptionSource
    from app.services.subscription_membership import SubscriptionMembershipService

    try:
        async with async_session() as db:
            await _clear(db)
            first = await _seed_user(db, "repository_first")
            second = await _seed_user(db, "repository_second")
            outsider = await _seed_user(db, "repository_outsider")
            creator = Creator(name="Repository Shared Artist")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="Canonical Repository")
            db.add(subscription)
            await db.flush()
            source = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id=f"repository-{uuid4().hex}",
                source_url="https://www.pixiv.net/users/82001",
                is_enabled=True,
            )
            db.add(source)
            await db.flush()
            first_member = await SubscriptionMembershipService(db, first.id).ensure_membership(
                subscription, name="First Repository Name"
            )
            second_member = await SubscriptionMembershipService(db, second.id).ensure_membership(
                subscription, name="Second Repository Name"
            )
            await SubscriptionMembershipService(db, first.id).ensure_source_binding(
                first_member, source, is_enabled=True
            )
            await SubscriptionMembershipService(db, second.id).ensure_source_binding(
                second_member, source, is_enabled=False
            )
            await db.commit()
            source_id = source.id
            first_name = first.username
            second_name = second.username
            outsider_name = outsider.username

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first_response = await client.get(
                f"/api/v1/repositories/{source_id}", headers=_headers(first_name)
            )
            second_response = await client.get(
                f"/api/v1/repositories/{source_id}", headers=_headers(second_name)
            )
            outsider_response = await client.get(
                f"/api/v1/repositories/{source_id}", headers=_headers(outsider_name)
            )
            outsider_tags = await client.get(
                f"/api/v1/repositories/{source_id}/tags", headers=_headers(outsider_name)
            )
        assert first_response.status_code == 200, first_response.text
        assert first_response.json()["repository"]["is_enabled"] is True
        assert first_response.json()["subscription"]["name"] == "First Repository Name"
        assert second_response.status_code == 200, second_response.text
        assert second_response.json()["repository"]["is_enabled"] is False
        assert second_response.json()["subscription"]["name"] == "Second Repository Name"
        assert outsider_response.status_code == 404
        assert outsider_tags.status_code == 404

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            removed = await client.delete(
                f"/api/v1/repositories/{source_id}", headers=_headers(first_name)
            )
            first_after = await client.get(
                f"/api/v1/repositories/{source_id}", headers=_headers(first_name)
            )
            second_after = await client.get(
                f"/api/v1/repositories/{source_id}", headers=_headers(second_name)
            )
        assert removed.status_code == 200, removed.text
        assert first_after.status_code == 404
        assert second_after.status_code == 200
        async with async_session() as db:
            assert await db.get(SubscriptionSource, source_id) is not None
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_task_and_download_lists_hide_other_users_private_triggers():
    """Opaque task/job triggers must enforce ownership on list and detail reads."""
    from httpx import ASGITransport, AsyncClient

    from app.database import async_session, engine
    from app.main import app
    from app.models import Creator, DownloadJob, Subscription, SubscriptionSource, TaskRun
    from app.services.download import DownloadService
    from app.services.subscription_membership import SubscriptionMembershipService
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear(db)
            first = await _seed_user(db, "task_first")
            second = await _seed_user(db, "task_second")
            creator = Creator(name="Private Trigger Artist")
            db.add(creator)
            await db.flush()
            canonical = Subscription(creator_id=creator.id, name="Private Trigger")
            db.add(canonical)
            await db.flush()
            source = SubscriptionSource(
                subscription_id=canonical.id,
                source="pixiv",
                source_creator_id="9911",
                source_url="https://www.pixiv.net/users/9911",
            )
            db.add(source)
            await db.flush()
            first_member = await SubscriptionMembershipService(db, first.id).ensure_membership(canonical)
            second_member = await SubscriptionMembershipService(db, second.id).ensure_membership(canonical)
            first_job = DownloadJob(
                subscription_id=canonical.id,
                subscription_source_id=source.id,
                triggering_user_subscription_id=first_member.id,
                source="pixiv",
                source_url=source.source_url,
                status="enqueued",
                owner_user_id=first.id,
            )
            second_job = DownloadJob(
                subscription_id=canonical.id,
                subscription_source_id=source.id,
                triggering_user_subscription_id=second_member.id,
                source="pixiv",
                source_url=source.source_url,
                # Shared sources permit only one active download. A terminal
                # historical job still exercises private list/detail filtering
                # without constructing a state production cannot persist.
                status="complete",
                owner_user_id=second.id,
            )
            db.add_all([first_job, second_job])
            await db.flush()
            first_task = TaskRun(
                kind="download",
                operation_type="download",
                subject_type="download_job",
                subject_id=first_job.id,
                status="enqueued",
                triggering_user_subscription_id=first_member.id,
                title="First private task",
                owner_user_id=first.id,
            )
            second_task = TaskRun(
                kind="download",
                operation_type="download",
                subject_type="download_job",
                subject_id=second_job.id,
                status="enqueued",
                triggering_user_subscription_id=second_member.id,
                title="Second private task",
                attention_state="open",
                owner_user_id=second.id,
            )
            db.add_all([first_task, second_task])
            await db.commit()

            total, tasks = await TaskService(db).list_tasks(
                include_account=True, user_id=first.id
            )
            assert total == 1
            assert [task.title for task in tasks] == ["First private task"]
            jobs = await DownloadService(db).list_jobs(user_id=first.id)
            assert [job.id for job in jobs] == [first_job.id]
            with pytest.raises(ValueError, match="DownloadJob not found"):
                await DownloadService(db).get_job(second_job.id, user_id=first.id)
            first_name = first.username
            first_job_id = first_job.id
            second_job_id = second_job.id
            first_task_id = first_task.id

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers = _headers(first_name)
            tasks_response = await client.get(
                "/api/v1/tasks", params={"include_account": "true"}, headers=headers
            )
            assert tasks_response.status_code == 200, tasks_response.text
            titles = {item["title"] for item in tasks_response.json()["items"]}
            assert "First private task" in titles
            assert "Second private task" not in titles
            anomalies_response = await client.get(
                "/api/v1/tasks/anomalies", headers=headers
            )
            assert anomalies_response.status_code == 200, anomalies_response.text
            anomaly_task_ids = {
                item["task_id"]
                for item in anomalies_response.json()["items"]
                if item.get("task_id")
            }
            assert str(second_task.id) not in anomaly_task_ids
            denied_task_mutation = await client.post(
                f"/api/v1/tasks/{second_task.id}/acknowledge", headers=headers
            )
            assert denied_task_mutation.status_code == 404
            jobs_response = await client.get("/api/v1/download-jobs", headers=headers)
            assert jobs_response.status_code == 200, jobs_response.text
            assert [item["id"] for item in jobs_response.json()] == [str(first_job_id)]
            assert (
                await client.get(f"/api/v1/download-jobs/{second_job_id}", headers=headers)
            ).status_code == 404
            summaries = await client.get(
                "/api/v1/subscriptions/summaries",
                params={"ids": str(canonical.id)},
                headers=headers,
            )
            assert summaries.status_code == 200, summaries.text
            assert summaries.json()["items"][0]["latest_state"]["task_id"] == str(
                first_task_id
            )
            denied_mutation = await client.post(
                f"/api/v1/download-jobs/{second_job_id}/priority",
                json={"priority": 1},
                headers=headers,
            )
            assert denied_mutation.status_code == 404
            empty_batch = await client.post(
                "/api/v1/download-jobs/batch-by-filter",
                json={"filters": {"source": "x"}, "action": "pause"},
                headers=headers,
            )
            assert empty_batch.status_code == 200, empty_batch.text
            assert empty_batch.json()["total_matched"] == 0

        async with async_session() as db:
            first_stored = await db.get(DownloadJob, first_job_id)
            second_stored = await db.get(DownloadJob, second_job_id)
            assert first_stored.status == "enqueued"
            assert second_stored.status == "complete"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_task_visibility_follows_download_and_import_membership_subjects():
    """Null-trigger legacy tasks inherit ownership through their domain job subject."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete

    from app.database import async_session, engine
    from app.main import app
    from app.models import (
        Creator,
        DownloadJob,
        ImportJob,
        Subscription,
        SubscriptionSource,
        TaskEvent,
        TaskRun,
    )
    from app.services.subscription_membership import SubscriptionMembershipService

    task_ids = []
    import_ids = []
    job_ids = []
    try:
        async with async_session() as db:
            await _clear(db)
            first = await _seed_user(db, "legacy_task_first")
            second = await _seed_user(db, "legacy_task_second")
            first_creator = Creator(name="Legacy First Artist")
            second_creator = Creator(name="Legacy Second Artist")
            db.add_all([first_creator, second_creator])
            await db.flush()
            first_subscription = Subscription(creator_id=first_creator.id, name="Legacy First")
            second_subscription = Subscription(creator_id=second_creator.id, name="Legacy Second")
            db.add_all([first_subscription, second_subscription])
            await db.flush()
            first_source = SubscriptionSource(
                subscription_id=first_subscription.id,
                source="pixiv",
                source_creator_id=f"legacy-first-{uuid4().hex}",
                source_url="https://www.pixiv.net/users/81001",
            )
            second_source = SubscriptionSource(
                subscription_id=second_subscription.id,
                source="pixiv",
                source_creator_id=f"legacy-second-{uuid4().hex}",
                source_url="https://www.pixiv.net/users/81002",
            )
            db.add_all([first_source, second_source])
            await db.flush()
            await SubscriptionMembershipService(db, first.id).ensure_membership(first_subscription)
            await SubscriptionMembershipService(db, second.id).ensure_membership(second_subscription)
            first_job = DownloadJob(
                subscription_id=first_subscription.id,
                subscription_source_id=first_source.id,
                source="pixiv",
                source_url=first_source.source_url,
                status="enqueued",
            )
            second_job = DownloadJob(
                subscription_id=second_subscription.id,
                subscription_source_id=second_source.id,
                source="pixiv",
                source_url=second_source.source_url,
                status="enqueued",
            )
            db.add_all([first_job, second_job])
            await db.flush()
            second_import = ImportJob(download_job_id=second_job.id, status="enqueued")
            db.add(second_import)
            await db.flush()
            first_task = TaskRun(
                kind="download",
                operation_type="download",
                subject_type="download_job",
                subject_id=first_job.id,
                status="enqueued",
                title="Legacy First Download",
            )
            second_task = TaskRun(
                kind="download",
                operation_type="download",
                subject_type="download_job",
                subject_id=second_job.id,
                status="enqueued",
                title="Legacy Second Download",
            )
            second_import_task = TaskRun(
                kind="import",
                operation_type="import",
                subject_type="import_job",
                subject_id=second_import.id,
                status="enqueued",
                title="Legacy Second Import",
            )
            second_subscription_task = TaskRun(
                kind="admin",
                operation_type="subscription-sync-batch",
                status="failed",
                attention_state="open",
                title="Legacy Second Subscription Batch",
                meta={"subscription_id": str(second_subscription.id)},
            )
            db.add_all(
                [first_task, second_task, second_import_task, second_subscription_task]
            )
            await db.commit()
            task_ids = [
                first_task.id,
                second_task.id,
                second_import_task.id,
                second_subscription_task.id,
            ]
            import_ids = [second_import.id]
            job_ids = [first_job.id, second_job.id]
            first_name = first.username

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers = _headers(first_name)
            for params in ({"include_account": "true"}, {"q": "Legacy"}):
                response = await client.get("/api/v1/tasks", params=params, headers=headers)
                assert response.status_code == 200, response.text
                titles = {item["title"] for item in response.json()["items"]}
                assert "Legacy First Download" in titles
                assert "Legacy Second Download" not in titles
                assert "Legacy Second Import" not in titles
                assert "Legacy Second Subscription Batch" not in titles
            for endpoint in ("/api/v1/tasks/anomalies", "/api/v1/operations/overview"):
                response = await client.get(endpoint, headers=headers)
                assert response.status_code == 200, response.text
                assert "Legacy Second Subscription Batch" not in {
                    item["title"] for item in response.json()["items"]
                }
            assert (
                await client.get(f"/api/v1/tasks/{second_task.id}", headers=headers)
            ).status_code == 404
            assert (
                await client.get(f"/api/v1/tasks/{second_import_task.id}", headers=headers)
            ).status_code == 404
            assert (
                await client.get(
                    f"/api/v1/tasks/{second_subscription_task.id}", headers=headers
                )
            ).status_code == 404
            assert (
                await client.post(
                    f"/api/v1/tasks/{second_subscription_task.id}/retry", headers=headers
                )
            ).status_code == 404
    finally:
        async with async_session() as db:
            if task_ids:
                await db.execute(delete(TaskEvent).where(TaskEvent.task_run_id.in_(task_ids)))
                await db.execute(delete(TaskRun).where(TaskRun.id.in_(task_ids)))
            if import_ids:
                await db.execute(delete(ImportJob).where(ImportJob.id.in_(import_ids)))
            if job_ids:
                await db.execute(delete(DownloadJob).where(DownloadJob.id.in_(job_ids)))
            await db.commit()
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_null_trigger_tasks_reuse_explicit_parent_job_owner_on_shared_subscription():
    """A peer member cannot inherit a null TaskRun owned by the parent job's trigger."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete

    from app.database import async_session, engine
    from app.main import app
    from app.models import (
        Creator,
        DownloadJob,
        ImportJob,
        RemoteAccount,
        Subscription,
        SubscriptionSource,
        TaskEvent,
        TaskRun,
    )
    from app.services.subscription_membership import SubscriptionMembershipService

    task_ids = []
    import_id = None
    job_id = None
    try:
        async with async_session() as db:
            await _clear(db)
            owner = await _seed_user(db, "legacy_explicit_owner")
            peer = await _seed_user(db, "legacy_explicit_peer")
            creator = Creator(name="Explicit Parent Owner")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="Shared Explicit")
            db.add(subscription)
            await db.flush()
            source = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id=f"explicit-{uuid4().hex}",
                source_url="https://www.pixiv.net/users/82001",
            )
            account = RemoteAccount(
                user_id=owner.id,
                source="pixiv",
                auth_method="refresh_token",
                auth_status="healthy",
            )
            db.add_all([source, account])
            await db.flush()
            owner_member = await SubscriptionMembershipService(
                db, owner.id
            ).ensure_membership(subscription)
            await SubscriptionMembershipService(db, peer.id).ensure_membership(subscription)
            job = DownloadJob(
                subscription_id=subscription.id,
                subscription_source_id=source.id,
                source="pixiv",
                source_url=source.source_url,
                status="enqueued",
                triggering_user_subscription_id=owner_member.id,
                triggering_remote_account_id=account.id,
                owner_user_id=owner.id,
            )
            db.add(job)
            await db.flush()
            import_job = ImportJob(download_job_id=job.id, status="enqueued")
            db.add(import_job)
            await db.flush()
            download_task = TaskRun(
                kind="download",
                operation_type="download",
                subject_type="download_job",
                subject_id=job.id,
                status="enqueued",
                title="Explicit Owner Legacy Download",
            )
            import_task = TaskRun(
                kind="import",
                operation_type="import",
                subject_type="import_job",
                subject_id=import_job.id,
                status="enqueued",
                title="Explicit Owner Legacy Import",
            )
            db.add_all([download_task, import_task])
            await db.commit()
            task_ids = [download_task.id, import_task.id]
            import_id = import_job.id
            job_id = job.id
            owner_name, peer_name = owner.username, peer.username

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for username, expected_titles in (
                (
                    owner_name,
                    {"Explicit Owner Legacy Download", "Explicit Owner Legacy Import"},
                ),
                (peer_name, set()),
            ):
                response = await client.get(
                    "/api/v1/tasks",
                    params={"q": "Explicit Owner Legacy"},
                    headers=_headers(username),
                )
                assert response.status_code == 200, response.text
                assert {item["title"] for item in response.json()["items"]} == expected_titles
            for task_id in task_ids:
                assert (
                    await client.get(
                        f"/api/v1/tasks/{task_id}", headers=_headers(owner_name)
                    )
                ).status_code == 200
                assert (
                    await client.get(
                        f"/api/v1/tasks/{task_id}", headers=_headers(peer_name)
                    )
                ).status_code == 404
    finally:
        async with async_session() as db:
            if task_ids:
                await db.execute(delete(TaskEvent).where(TaskEvent.task_run_id.in_(task_ids)))
                await db.execute(delete(TaskRun).where(TaskRun.id.in_(task_ids)))
            if import_id:
                await db.execute(delete(ImportJob).where(ImportJob.id == import_id))
            if job_id:
                await db.execute(delete(DownloadJob).where(DownloadJob.id == job_id))
            await db.commit()
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_global_scheduler_batch_is_visible_only_to_system_or_admin_users():
    """A scheduler-wide batch is a system audit task, not a member-scoped batch."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete

    from app.database import async_session, engine
    from app.main import app
    from app.models import TaskEvent, TaskRun

    task_id = None
    try:
        async with async_session() as db:
            await _clear(db)
            ordinary = await _seed_user(db, "global_batch_ordinary")
            system_user = await _seed_user(db, "global_batch_system")
            system_user.permissions = [*system_user.permissions, "system"]
            admin = await _seed_user(db, "global_batch_admin", admin=True)
            task = TaskRun(
                kind="admin",
                operation_type="subscription-sync-batch",
                status="failed",
                attention_state="open",
                title="Global Scheduler Batch",
                meta={"scheduled_for": "2026-08-28T00:00:00+00:00", "source_count": 809},
            )
            db.add(task)
            await db.commit()
            task_id = task.id
            names = (ordinary.username, system_user.username, admin.username)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ordinary_headers = _headers(names[0])
            for endpoint in (
                "/api/v1/tasks?include_account=true",
                "/api/v1/tasks?q=Global%20Scheduler",
                "/api/v1/tasks/anomalies",
                "/api/v1/operations/overview",
            ):
                response = await client.get(endpoint, headers=ordinary_headers)
                assert response.status_code == 200, response.text
                assert "Global Scheduler Batch" not in response.text
            assert (
                await client.get(f"/api/v1/tasks/{task_id}", headers=ordinary_headers)
            ).status_code == 403
            assert (
                await client.post(
                    f"/api/v1/tasks/{task_id}/retry", headers=ordinary_headers
                )
            ).status_code == 403

            for username in names[1:]:
                headers = _headers(username)
                detail = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
                assert detail.status_code == 200, detail.text
                listed = await client.get(
                    "/api/v1/tasks", params={"q": "Global Scheduler"}, headers=headers
                )
                assert listed.status_code == 200, listed.text
                assert [item["id"] for item in listed.json()["items"]] == [str(task_id)]
                overview = await client.get("/api/v1/tasks/anomalies", headers=headers)
                assert overview.status_code == 200
                assert "Global Scheduler Batch" in overview.text
                control = await client.post(
                    f"/api/v1/tasks/{task_id}/retry", headers=headers
                )
                assert control.status_code == 409, control.text
                assert control.json()["detail"]["code"] == "invalid_task_action"
    finally:
        async with async_session() as db:
            if task_id:
                await db.execute(delete(TaskEvent).where(TaskEvent.task_run_id == task_id))
                await db.execute(delete(TaskRun).where(TaskRun.id == task_id))
                await db.commit()
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_locatorless_subscription_batches_with_triggers_are_owner_scoped():
    """A trigger-owned batch cannot be promoted to a scheduler-global audit task."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete

    from app.database import async_session, engine
    from app.main import app
    from app.models import (
        Creator,
        RemoteAccount,
        Subscription,
        TaskEvent,
        TaskRun,
    )
    from app.services.subscription_membership import SubscriptionMembershipService

    task_ids = []
    try:
        async with async_session() as db:
            await _clear(db)
            owner = await _seed_user(db, "trigger_batch_owner")
            ordinary = await _seed_user(db, "trigger_batch_ordinary")
            system_user = await _seed_user(db, "trigger_batch_system")
            system_user.permissions = [*system_user.permissions, "system"]
            admin = await _seed_user(db, "trigger_batch_admin", admin=True)
            creator = Creator(name="Trigger Batch Artist")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="Trigger Batch Shared")
            db.add(subscription)
            await db.flush()
            membership = await SubscriptionMembershipService(
                db, owner.id
            ).ensure_membership(subscription)
            account = RemoteAccount(
                user_id=owner.id,
                source="pixiv",
                auth_method="refresh_token",
                auth_status="healthy",
            )
            db.add(account)
            await db.flush()
            member_task = TaskRun(
                kind="admin",
                operation_type="subscription-sync-batch",
                triggering_user_subscription_id=membership.id,
                status="failed",
                attention_state="open",
                title="Scoped Trigger Batch Membership",
                meta={"mode": "legacy"},
                owner_user_id=owner.id,
            )
            account_task = TaskRun(
                kind="admin",
                operation_type="subscription-sync-batch",
                triggering_remote_account_id=account.id,
                status="failed",
                attention_state="open",
                title="Scoped Trigger Batch Account",
                meta={"mode": "legacy"},
                owner_user_id=owner.id,
            )
            non_admin_task = TaskRun(
                kind="download",
                operation_type="subscription-sync-batch",
                status="failed",
                attention_state="open",
                title="Scoped Trigger Batch Non Admin",
                meta={"mode": "legacy"},
            )
            db.add_all([member_task, account_task, non_admin_task])
            await db.commit()
            task_ids = [member_task.id, account_task.id, non_admin_task.id]
            owner_name = owner.username
            unrelated_names = (
                ordinary.username,
                system_user.username,
                admin.username,
            )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            owner_headers = _headers(owner_name)
            owner_list = await client.get(
                "/api/v1/tasks",
                params={"q": "Scoped Trigger Batch"},
                headers=owner_headers,
            )
            assert owner_list.status_code == 200, owner_list.text
            assert {item["id"] for item in owner_list.json()["items"]} == {
                str(member_task.id),
                str(account_task.id),
            }
            for task in (member_task, account_task):
                detail = await client.get(
                    f"/api/v1/tasks/{task.id}", headers=owner_headers
                )
                assert detail.status_code == 200, detail.text
                control = await client.post(
                    f"/api/v1/tasks/{task.id}/retry", headers=owner_headers
                )
                assert control.status_code == 409, control.text
                assert control.json()["detail"]["code"] == "invalid_task_action"
            assert (
                await client.get(
                    f"/api/v1/tasks/{non_admin_task.id}", headers=owner_headers
                )
            ).status_code == 404

            for username in unrelated_names:
                headers = _headers(username)
                listed = await client.get(
                    "/api/v1/tasks",
                    params={"q": "Scoped Trigger Batch"},
                    headers=headers,
                )
                assert listed.status_code == 200, listed.text
                assert listed.json()["items"] == []
                for task_id in task_ids:
                    assert (
                        await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
                    ).status_code == 404
                    assert (
                        await client.post(
                            f"/api/v1/tasks/{task_id}/retry", headers=headers
                        )
                    ).status_code == 404
    finally:
        async with async_session() as db:
            if task_ids:
                await db.execute(delete(TaskEvent).where(TaskEvent.task_run_id.in_(task_ids)))
                await db.execute(delete(TaskRun).where(TaskRun.id.in_(task_ids)))
                await db.commit()
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_falsey_meta_locators_do_not_create_global_subscription_batches():
    """Every non-NULL legacy locator blocks global system/admin task access."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete

    from app.database import async_session, engine
    from app.main import app
    from app.models import TaskEvent, TaskRun

    task_ids = []
    try:
        async with async_session() as db:
            await _clear(db)
            ordinary = await _seed_user(db, "falsey_locator_ordinary")
            system_user = await _seed_user(db, "falsey_locator_system")
            system_user.permissions = [*system_user.permissions, "system"]
            admin = await _seed_user(db, "falsey_locator_admin", admin=True)
            falsey_tasks = [
                TaskRun(
                    kind="admin",
                    operation_type="subscription-sync-batch",
                    status="failed",
                    attention_state="open",
                    title=f"Falsey Locator Batch {locator}",
                    meta={locator: value},
                )
                for locator, value in (
                    ("subscription_id", ""),
                    ("subscription_source_id", 0),
                    ("user_subscription_id", False),
                    ("remote_account_id", ""),
                )
            ]
            json_null_global = TaskRun(
                kind="admin",
                operation_type="subscription-sync-batch",
                status="failed",
                attention_state="open",
                title="JSON Null Locator Global Batch",
                meta={
                    "subscription_id": None,
                    "subscription_source_id": None,
                    "user_subscription_id": None,
                    "remote_account_id": None,
                },
            )
            db.add_all([*falsey_tasks, json_null_global])
            await db.commit()
            task_ids = [task.id for task in falsey_tasks] + [json_null_global.id]
            ordinary_name = ordinary.username
            privileged_names = (system_user.username, admin.username)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for username in (ordinary_name, *privileged_names):
                headers = _headers(username)
                falsey_list = await client.get(
                    "/api/v1/tasks",
                    params={"q": "Falsey Locator Batch"},
                    headers=headers,
                )
                assert falsey_list.status_code == 200, falsey_list.text
                assert falsey_list.json()["items"] == []
                for task in falsey_tasks:
                    assert (
                        await client.get(f"/api/v1/tasks/{task.id}", headers=headers)
                    ).status_code == 404
                    assert (
                        await client.post(
                            f"/api/v1/tasks/{task.id}/acknowledge", headers=headers
                        )
                    ).status_code == 404
                    assert (
                        await client.post(
                            f"/api/v1/tasks/{task.id}/retry", headers=headers
                        )
                    ).status_code == 404

            ordinary_headers = _headers(ordinary_name)
            assert (
                await client.get(
                    f"/api/v1/tasks/{json_null_global.id}", headers=ordinary_headers
                )
            ).status_code == 403
            assert (
                await client.post(
                    f"/api/v1/tasks/{json_null_global.id}/acknowledge",
                    headers=ordinary_headers,
                )
            ).status_code == 403
            assert (
                await client.post(
                    f"/api/v1/tasks/{json_null_global.id}/retry",
                    headers=ordinary_headers,
                )
            ).status_code == 403

            system_headers = _headers(privileged_names[0])
            global_list = await client.get(
                "/api/v1/tasks",
                params={"q": "JSON Null Locator Global Batch"},
                headers=system_headers,
            )
            assert global_list.status_code == 200, global_list.text
            assert [item["id"] for item in global_list.json()["items"]] == [
                str(json_null_global.id)
            ]
            assert (
                await client.get(
                    f"/api/v1/tasks/{json_null_global.id}", headers=system_headers
                )
            ).status_code == 200
            assert (
                await client.post(
                    f"/api/v1/tasks/{json_null_global.id}/acknowledge",
                    headers=system_headers,
                )
            ).status_code == 200
            control = await client.post(
                f"/api/v1/tasks/{json_null_global.id}/retry", headers=system_headers
            )
            assert control.status_code == 409, control.text
            assert control.json()["detail"]["code"] == "invalid_task_action"
    finally:
        async with async_session() as db:
            if task_ids:
                await db.execute(delete(TaskEvent).where(TaskEvent.task_run_id.in_(task_ids)))
                await db.execute(delete(TaskRun).where(TaskRun.id.in_(task_ids)))
                await db.commit()
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_job_routes_and_global_reconciliation_are_owner_scoped():
    """A tasks-only user cannot read/control peer imports or run global scans."""
    from httpx import ASGITransport, AsyncClient

    from app.database import async_session, engine
    from app.main import app
    from app.models import Creator, DownloadJob, ImportJob, Subscription, SubscriptionSource
    from app.services.subscription_membership import SubscriptionMembershipService

    try:
        async with async_session() as db:
            await _clear(db)
            first = await _seed_user(db, "import_route_first")
            second = await _seed_user(db, "import_route_second")
            creator = Creator(name="Import Route Artist")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="Import Route Shared")
            db.add(subscription)
            await db.flush()
            source = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id=f"import-route-{uuid4().hex}",
                source_url="https://www.pixiv.net/users/991122",
            )
            db.add(source)
            await db.flush()
            first_member = await SubscriptionMembershipService(
                db, first.id
            ).ensure_membership(subscription)
            second_member = await SubscriptionMembershipService(
                db, second.id
            ).ensure_membership(subscription)
            first_download = DownloadJob(
                subscription_id=subscription.id,
                subscription_source_id=source.id,
                triggering_user_subscription_id=first_member.id,
                source="pixiv",
                source_url=source.source_url,
                status="complete",
                owner_user_id=first.id,
            )
            second_download = DownloadJob(
                subscription_id=subscription.id,
                subscription_source_id=source.id,
                triggering_user_subscription_id=second_member.id,
                source="pixiv",
                source_url=source.source_url,
                status="complete",
                owner_user_id=second.id,
            )
            db.add_all([first_download, second_download])
            await db.flush()
            own_import = ImportJob(download_job_id=first_download.id, status="complete")
            peer_retry = ImportJob(download_job_id=second_download.id, status="complete")
            peer_cancel = ImportJob(download_job_id=second_download.id, status="complete")
            peer_priority = ImportJob(
                download_job_id=second_download.id,
                status="complete",
                priority=10,
            )
            peer_batch = ImportJob(download_job_id=second_download.id, status="complete")
            peer_delete = ImportJob(download_job_id=second_download.id, status="complete")
            db.add_all(
                [
                    own_import,
                    peer_retry,
                    peer_cancel,
                    peer_priority,
                    peer_batch,
                    peer_delete,
                ]
            )
            await db.commit()
            first_name = first.username
            own_id = own_import.id
            peer_ids = {
                "retry": peer_retry.id,
                "cancel": peer_cancel.id,
                "priority": peer_priority.id,
                "batch": peer_batch.id,
                "delete": peer_delete.id,
            }

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers = _headers(first_name)
            listed = await client.get("/api/v1/import-jobs", headers=headers)
            assert listed.status_code == 200, listed.text
            assert listed.json()["total"] == 1
            assert [item["id"] for item in listed.json()["items"]] == [str(own_id)]
            assert (
                await client.get(
                    f"/api/v1/import-jobs/{peer_ids['priority']}", headers=headers
                )
            ).status_code == 404
            assert (
                await client.post(
                    f"/api/v1/import-jobs/{peer_ids['retry']}/retry", headers=headers
                )
            ).status_code == 404
            assert (
                await client.post(
                    f"/api/v1/import-jobs/{peer_ids['cancel']}/cancel", headers=headers
                )
            ).status_code == 404
            assert (
                await client.post(
                    f"/api/v1/import-jobs/{peer_ids['priority']}/priority",
                    json={"priority": 77},
                    headers=headers,
                )
            ).status_code == 404
            batch = await client.post(
                "/api/v1/import-jobs/batch-by-filter",
                json={
                    "filters": {"ids": [str(peer_ids["batch"])]},
                    "action": "pause",
                },
                headers=headers,
            )
            assert batch.status_code == 200, batch.text
            assert batch.json()["total_matched"] == 0
            assert (
                await client.delete(
                    f"/api/v1/import-jobs/{peer_ids['delete']}", headers=headers
                )
            ).status_code == 404
            assert (
                await client.post("/api/v1/import-jobs/scan", headers=headers)
            ).status_code == 403
            assert (
                await client.post(
                    "/api/v1/tasks/reconcile",
                    json={"dry_run": True, "limit": 1},
                    headers=headers,
                )
            ).status_code == 403

        async with async_session() as db:
            stored = await db.get(ImportJob, peer_ids["priority"])
            assert stored is not None
            assert stored.priority == 10
            assert await db.get(ImportJob, peer_ids["delete"]) is not None
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subscription_query_accepts_real_dict_search_hits(monkeypatch):
    """The subscription compatibility endpoint consumes SearchService dict items."""
    from httpx import ASGITransport, AsyncClient

    from app.database import async_session, engine
    from app.main import app
    from app.models import Creator, Subscription
    from app.services.search import SearchService
    from app.services.subscription_membership import SubscriptionMembershipService

    try:
        async with async_session() as db:
            await _clear(db)
            user = await _seed_user(db, "dict_search_hit")
            creator = Creator(name="Dict Search Creator")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="Canonical Dict Name")
            db.add(subscription)
            await db.flush()
            member = await SubscriptionMembershipService(db, user.id).ensure_membership(
                subscription, name="Private Dict Name"
            )
            await db.commit()
            username = user.username
            subscription_id = subscription.id
            membership_id = member.id

        async def fake_search(self, query, offset, limit, **kwargs):
            return {
                "groups": {
                    "subscriptions": {
                        "total": 1,
                        "items": [{"id": str(subscription_id), "name": "Indexed Name"}],
                    }
                }
            }

        monkeypatch.setattr(SearchService, "search", fake_search)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/v1/subscriptions",
                params={"q": "Dict"},
                headers=_headers(username),
            )
        assert response.status_code == 200, response.text
        assert len(response.json()) == 1
        assert response.json()[0]["id"] == str(subscription_id)
        assert response.json()[0]["membership_id"] == str(membership_id)
        assert response.json()[0]["name"] == "Private Dict Name"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_assist_and_scheduler_use_private_source_bindings_before_pagination():
    """Repo suggestions and scheduler rows cannot be crowded or overlaid by peers."""
    from datetime import datetime, timedelta, timezone

    from httpx import ASGITransport, AsyncClient

    from app.database import async_session, engine
    from app.main import app
    from app.models import Creator, Subscription, SubscriptionSource
    from app.services.subscription_membership import SubscriptionMembershipService

    try:
        async with async_session() as db:
            await _clear(db)
            first = await _seed_user(db, "search_policy_first")
            second = await _seed_user(db, "search_policy_second")

            peer_creator = Creator(name="Peer Search Creator")
            shared_creator = Creator(name="Shared Search Creator")
            db.add_all([peer_creator, shared_creator])
            await db.flush()
            peer_subscription = Subscription(
                creator_id=peer_creator.id,
                name="Peer Canonical Subscription",
            )
            shared_subscription = Subscription(
                creator_id=shared_creator.id,
                name="Shared Canonical Subscription",
            )
            db.add_all([peer_subscription, shared_subscription])
            await db.flush()
            # Insert the peer row first so an unfiltered LIMIT 1 crowds out the
            # current user's later repository suggestion.
            peer_source = SubscriptionSource(
                subscription_id=peer_subscription.id,
                source="pixiv",
                source_creator_id="crowd-assist-peer",
                source_url="https://www.pixiv.net/users/44001",
            )
            shared_source = SubscriptionSource(
                subscription_id=shared_subscription.id,
                source="pixiv",
                source_creator_id="crowd-assist-own",
                source_url="https://www.pixiv.net/users/44002",
            )
            db.add_all([peer_source, shared_source])
            await db.flush()
            peer_member = await SubscriptionMembershipService(
                db, second.id
            ).ensure_membership(peer_subscription, name="Peer Private Subscription")
            first_member = await SubscriptionMembershipService(
                db, first.id
            ).ensure_membership(shared_subscription, name="First Private Subscription")
            second_member = await SubscriptionMembershipService(
                db, second.id
            ).ensure_membership(shared_subscription, name="Second Private Subscription")
            await SubscriptionMembershipService(db, second.id).ensure_source_binding(
                peer_member, peer_source, is_enabled=True
            )
            first_binding = await SubscriptionMembershipService(
                db, first.id
            ).ensure_source_binding(first_member, shared_source, is_enabled=False)
            await SubscriptionMembershipService(db, second.id).ensure_source_binding(
                second_member, shared_source, is_enabled=True
            )
            first_binding.auth_healthy = False
            first_binding.auth_status = "unhealthy"
            first_binding.next_sync_at = datetime.now(timezone.utc) + timedelta(hours=9)
            await db.commit()
            first_name = first.username
            shared_source_id = shared_source.id

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers = _headers(first_name)
            assist = await client.post(
                "/api/v1/search/assist",
                json={
                    "before_cursor": "repo:crowd-assist",
                    "after_cursor": "",
                    "scope": "global",
                    "limit": 1,
                },
                headers=headers,
            )
            assert assist.status_code == 200, assist.text
            assert len(assist.json()["suggestions"]) == 1
            assert "crowd-assist-own" in assist.json()["suggestions"][0]["label"]

            scheduler = await client.get(
                "/api/v1/search",
                params={"scope": "scheduler", "limit": 100},
                headers=headers,
            )
            assert scheduler.status_code == 200, scheduler.text
            group = scheduler.json()["groups"]["scheduler"]
            assert group["total"] == 1
            assert len(group["items"]) == 1
            item = group["items"][0]
            assert item["source_id"] == str(shared_source_id)
            assert item["subscription_name"] == "First Private Subscription"
            assert item["source_enabled"] is False
            assert item["auth_healthy"] is False
            assert item["decision"] == "source_disabled"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subscription_summary_selects_latest_receipt_only_from_owned_sources():
    """A newer peer-only source receipt cannot replace the member's own result."""
    from datetime import datetime, timedelta, timezone

    from httpx import ASGITransport, AsyncClient

    from app.database import async_session, engine
    from app.main import app
    from app.models import (
        Creator,
        RepositorySyncReceipt,
        Subscription,
        SubscriptionSource,
    )
    from app.services.subscription_membership import SubscriptionMembershipService

    try:
        async with async_session() as db:
            await _clear(db)
            first = await _seed_user(db, "receipt_first")
            second = await _seed_user(db, "receipt_second")
            creator = Creator(name="Receipt Isolation Creator")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="Receipt Shared")
            db.add(subscription)
            await db.flush()
            own_source = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id=f"receipt-own-{uuid4().hex}",
                source_url="https://www.pixiv.net/users/55001",
            )
            peer_source = SubscriptionSource(
                subscription_id=subscription.id,
                source="x",
                source_creator_id=f"receipt-peer-{uuid4().hex}",
                source_url="https://x.com/receipt_peer",
            )
            db.add_all([own_source, peer_source])
            await db.flush()
            first_member = await SubscriptionMembershipService(
                db, first.id
            ).ensure_membership(subscription)
            second_member = await SubscriptionMembershipService(
                db, second.id
            ).ensure_membership(subscription)
            await SubscriptionMembershipService(db, first.id).ensure_source_binding(
                first_member, own_source
            )
            await SubscriptionMembershipService(db, second.id).ensure_source_binding(
                second_member, peer_source
            )
            now = datetime.now(timezone.utc)
            own_task_id = uuid4()
            peer_task_id = uuid4()
            db.add_all(
                [
                    RepositorySyncReceipt(
                        repository_id=own_source.id,
                        source_download_job_id=uuid4(),
                        source_task_id=own_task_id,
                        source="pixiv",
                        status="complete",
                        outcome_code="own_complete",
                        finished_at=now - timedelta(hours=1),
                    ),
                    RepositorySyncReceipt(
                        repository_id=peer_source.id,
                        source_download_job_id=uuid4(),
                        source_task_id=peer_task_id,
                        source="x",
                        status="complete",
                        outcome_code="peer_newer",
                        finished_at=now,
                    ),
                ]
            )
            await db.commit()
            first_name = first.username
            subscription_id = subscription.id
            own_source_id = own_source.id

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/v1/subscriptions/summaries",
                params={"ids": str(subscription_id)},
                headers=_headers(first_name),
            )
        assert response.status_code == 200, response.text
        latest = response.json()["items"][0]["latest_state"]
        assert latest["state"] == "success"
        assert latest["repository_id"] == str(own_source_id)
        assert latest["task_id"] == str(own_task_id)
        assert latest["outcome_code"] == "own_complete"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
