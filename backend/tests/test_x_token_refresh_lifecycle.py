"""Durable, redacted X OAuth refresh lifecycle."""

from __future__ import annotations

import base64
from collections import deque
from datetime import datetime, timezone
import logging
from uuid import uuid4

import pytest
from sqlalchemy import select, text


PREFIX = "x_refresh_lifecycle_"


def _vault():
    from app.services.remote_credentials import CredentialVault

    return CredentialVault(base64.urlsafe_b64encode(b"r" * 32).decode())


class FixtureTransport:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.requests = []

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected remote request")
        return self.responses.popleft()


class Registry:
    def __init__(self, adapter):
        self.adapter = adapter

    def get(self, source):
        assert source == "x"
        return self.adapter


async def _cleanup(db) -> None:
    await db.execute(
        text(
            "DELETE FROM task_events WHERE task_run_id IN ("
            "SELECT id FROM task_runs WHERE owner_user_id IN ("
            "SELECT id FROM users WHERE username LIKE :prefix))"
        ),
        {"prefix": f"{PREFIX}%"},
    )
    await db.execute(
        text(
            "DELETE FROM task_runs WHERE owner_user_id IN ("
            "SELECT id FROM users WHERE username LIKE :prefix)"
        ),
        {"prefix": f"{PREFIX}%"},
    )
    await db.execute(
        text(
            "DELETE FROM discovery_candidates WHERE user_id IN ("
            "SELECT id FROM users WHERE username LIKE :prefix)"
        ),
        {"prefix": f"{PREFIX}%"},
    )
    params = {"prefix": f"{PREFIX}%"}
    for table in ("user_subscription_sources", "remote_accounts", "user_subscriptions"):
        await db.execute(
            text(
                f"DELETE FROM {table} WHERE user_id IN ("
                "SELECT id FROM users WHERE username LIKE :prefix)"
            ),
            params,
        )
    await db.execute(
        text(
            "DELETE FROM subscription_sources WHERE subscription_id IN ("
            "SELECT s.id FROM subscriptions s JOIN creators c ON c.id=s.creator_id "
            "WHERE c.name LIKE :prefix)"
        ),
        params,
    )
    await db.execute(
        text(
            "DELETE FROM subscriptions WHERE creator_id IN ("
            "SELECT id FROM creators WHERE name LIKE :prefix)"
        ),
        params,
    )
    await db.execute(
        text("DELETE FROM creators WHERE name LIKE :prefix"),
        params,
    )
    await db.execute(
        text("DELETE FROM users WHERE username LIKE :prefix"),
        {"prefix": f"{PREFIX}%"},
    )
    await db.commit()


async def _create_x_account(db, adapter, *, suffix: str, credentials: dict[str, str]):
    from app.models import User
    from app.services.remote_accounts import RemoteAccountService

    user = User(
        username=f"{PREFIX}{suffix}_{uuid4().hex[:8]}",
        password_hash="test-only",
        is_active=True,
    )
    db.add(user)
    await db.flush()
    service = RemoteAccountService(
        db,
        user.id,
        vault=_vault(),
        adapters=Registry(adapter),
    )
    account = await service.create(
        {
            "source": "x",
            "auth_method": "oauth2",
            "remote_user_id": "42",
            "credentials": credentials,
            "collection_selectors": [{"kind": "following"}],
        }
    )
    return user, account, service


async def _bind_account(db, user, account, *, suffix: str):
    from app.models import (
        Creator,
        Subscription,
        SubscriptionSource,
        UserSubscription,
        UserSubscriptionSource,
    )
    from app.services.subscription_membership import (
        recompute_subscription_membership_cache,
    )

    creator = Creator(name=f"{PREFIX}{suffix}_{uuid4().hex[:8]}")
    db.add(creator)
    await db.flush()
    subscription = Subscription(creator_id=creator.id, name=creator.name)
    db.add(subscription)
    await db.flush()
    source = SubscriptionSource(
        subscription_id=subscription.id,
        source="x",
        source_creator_id=f"{suffix}_{uuid4().hex}",
        source_url=f"https://x.com/{suffix}",
    )
    membership = UserSubscription(
        user_id=user.id,
        subscription_id=subscription.id,
        name=creator.name,
    )
    db.add_all([source, membership])
    await db.flush()
    binding = UserSubscriptionSource(
        user_id=user.id,
        subscription_id=subscription.id,
        user_subscription_id=membership.id,
        subscription_source_id=source.id,
        remote_account_id=account.id,
        is_enabled=True,
        auth_healthy=True,
        auth_status="healthy",
        last_auth_checked_at=datetime.now(timezone.utc),
    )
    db.add(binding)
    await db.flush()
    await recompute_subscription_membership_cache(db, subscription.id)
    return binding


@pytest.mark.integration
@pytest.mark.asyncio
async def test_account_test_refresh_persists_rotated_tokens_and_generation(
    monkeypatch,
    caplog,
):
    """A recovered 401 durably rotates encrypted tokens before reporting healthy."""

    from app.config import settings
    from app.database import async_session, engine
    from app.models import RemoteAccount, TaskRun, User
    from app.remote_discovery.common import RemoteHTTPResponse
    from app.remote_discovery.x import XRemoteDiscoveryAdapter
    from app.services.remote_accounts import RemoteAccountService

    old_access = "stored-expired-access-canary"
    old_refresh = "stored-original-refresh-canary"
    rotated_access = "stored-rotated-access-canary"
    rotated_refresh = "stored-rotated-refresh-canary"
    client_id = "stored-client-id-canary"
    canaries = (old_access, old_refresh, rotated_access, rotated_refresh, client_id)
    transport = FixtureTransport(
        RemoteHTTPResponse(401, {"title": "expired"}),
        RemoteHTTPResponse(
            200,
            {"access_token": rotated_access, "refresh_token": rotated_refresh},
        ),
        RemoteHTTPResponse(200, {"data": {"id": "42"}}),
        RemoteHTTPResponse(
            200,
            {"data": {"id": "42", "name": "Owner", "username": "owner"}},
        ),
    )
    adapter = XRemoteDiscoveryAdapter(transport)
    monkeypatch.setattr(settings, "remote_discovery_private_members_enabled", True)
    monkeypatch.setattr(settings, "remote_discovery_x_enabled", True)
    caplog.set_level(logging.DEBUG)
    try:
        async with async_session() as db:
            await _cleanup(db)
            user = User(
                username=f"{PREFIX}{uuid4().hex[:10]}",
                password_hash="test-only",
                is_active=True,
            )
            db.add(user)
            await db.flush()
            service = RemoteAccountService(
                db,
                user.id,
                vault=_vault(),
                adapters=Registry(adapter),
            )
            created = await service.create(
                {
                    "source": "x",
                    "auth_method": "oauth2",
                    "remote_user_id": "42",
                    "credentials": {
                        "access_token": old_access,
                        "refresh_token": old_refresh,
                        "client_id": client_id,
                    },
                }
            )
            await db.commit()
            assert created.credential_mask == {
                "access_token": "••••",
                "client_id": "••••",
                "refresh_token": "••••",
            }

            refreshed = await service.test(created.id)
            await db.commit()
            stored = await db.get(RemoteAccount, created.id, populate_existing=True)
            decrypted = _vault().decrypt(
                stored.credential_ciphertext,
                user_id=user.id,
                source="x",
                account_id=stored.id,
            ).materialize()
            assert stored.credential_generation == 2
            assert decrypted == {
                "access_token": rotated_access,
                "refresh_token": rotated_refresh,
                "client_id": client_id,
            }
            assert refreshed.auth_status == "healthy"
            assert all(secret not in str(refreshed) for secret in canaries)
            assert all(secret not in caplog.text for secret in canaries)
            task_rows = list((await db.execute(select(TaskRun))).scalars())
            assert all(secret not in str(task) for task in task_rows for secret in canaries)
            from app.services.redis_client import get_redis

            redis_client = get_redis()
            encoded_canaries = tuple(secret.encode() for secret in canaries)
            for key in redis_client.scan_iter():
                dumped = redis_client.dump(key) or b""
                if any(canary in dumped for canary in encoded_canaries):
                    raise AssertionError("an OAuth credential reached Redis")
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collections_refreshes_once_and_persists_rotation_across_pages(monkeypatch):
    """Collection pagination shares the durable one-refresh lifecycle."""

    from app.config import settings
    from app.database import async_session, engine
    from app.models import RemoteAccount
    from app.remote_discovery.common import RemoteHTTPResponse
    from app.remote_discovery.x import XRemoteDiscoveryAdapter

    old_access = "collections-old-access-canary"
    old_refresh = "collections-old-refresh-canary"
    rotated_access = "collections-new-access-canary"
    rotated_refresh = "collections-new-refresh-canary"
    client_id = "collections-client-canary"
    transport = FixtureTransport(
        RemoteHTTPResponse(401, {"title": "expired"}),
        RemoteHTTPResponse(
            200,
            {
                "access_token": rotated_access,
                "refresh_token": rotated_refresh,
            },
        ),
        RemoteHTTPResponse(200, {"data": {"id": "42"}}),
        RemoteHTTPResponse(
            200,
            {
                "data": [{"id": "701", "name": "First list", "private": False}],
                "meta": {"next_token": "next-page"},
            },
        ),
        RemoteHTTPResponse(
            200,
            {
                "data": [{"id": "702", "name": "Second list", "private": True}],
                "meta": {},
            },
        ),
    )
    adapter = XRemoteDiscoveryAdapter(transport)
    monkeypatch.setattr(settings, "remote_discovery_private_members_enabled", True)
    monkeypatch.setattr(settings, "remote_discovery_x_enabled", True)
    try:
        async with async_session() as db:
            await _cleanup(db)
            user, account, service = await _create_x_account(
                db,
                adapter,
                suffix="collections",
                credentials={
                    "access_token": old_access,
                    "refresh_token": old_refresh,
                    "client_id": client_id,
                },
            )
            await db.commit()

            collections = await service.collections(account.id)
            assert [collection.id for collection in collections] == [
                "following",
                "list:701",
                "list:702",
            ]
            stored = await db.get(RemoteAccount, account.id, populate_existing=True)
            decrypted = _vault().decrypt(
                stored.credential_ciphertext,
                user_id=user.id,
                source="x",
                account_id=account.id,
            ).materialize()
            assert stored.credential_generation == 2
            assert decrypted == {
                "access_token": rotated_access,
                "refresh_token": rotated_refresh,
                "client_id": client_id,
            }
            assert sum(url.endswith("/oauth2/token") for _, url, _ in transport.requests) == 1
            page_requests = [
                kwargs
                for _, url, kwargs in transport.requests
                if url.endswith("/users/42/owned_lists")
            ]
            assert page_requests[-1]["params"]["pagination_token"] == "next-page"
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scan_refresh_advances_task_and_checkpoint_pin_before_next_page(
    monkeypatch,
):
    """A verified internal rotation advances every durable scan pin atomically."""

    import asyncio

    from app.config import settings
    from app.database import async_session, engine
    from app.models import RemoteAccount, TaskRun, User
    from app.remote_discovery.common import RemoteHTTPResponse
    from app.remote_discovery.x import XRemoteDiscoveryAdapter
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_discovery import RemoteDiscoveryService

    old_access = "scan-old-access-canary"
    old_refresh = "scan-old-refresh-canary"
    new_access = "scan-new-access-canary"
    new_refresh = "scan-new-refresh-canary"
    client_id = "scan-client-canary"
    canaries = (old_access, old_refresh, new_access, new_refresh, client_id)

    class BlockingSecondPageTransport:
        def __init__(self):
            self.following_requests = 0
            self.second_started = asyncio.Event()
            self.release_second = asyncio.Event()

        async def request(self, method, url, **kwargs):
            if url.endswith("/oauth2/token"):
                return RemoteHTTPResponse(
                    200,
                    {"access_token": new_access, "refresh_token": new_refresh},
                )
            if url.endswith("/users/me"):
                return RemoteHTTPResponse(200, {"data": {"id": "42"}})
            if url.endswith("/users/42/following"):
                self.following_requests += 1
                if self.following_requests == 1:
                    return RemoteHTTPResponse(401, {"title": "expired"})
                authorization = (kwargs.get("headers") or {}).get("Authorization")
                if authorization != f"Bearer {new_access}":
                    raise AssertionError("scan did not use the rotated access token")
                if self.following_requests == 2:
                    return RemoteHTTPResponse(
                        200,
                        {
                            "data": [
                                {"id": "501", "name": "First", "username": "first"}
                            ],
                            "meta": {"next_token": "page-two"},
                        },
                    )
                self.second_started.set()
                await self.release_second.wait()
                return RemoteHTTPResponse(
                    200,
                    {
                        "data": [
                            {"id": "502", "name": "Second", "username": "second"}
                        ],
                        "meta": {},
                    },
                )
            raise AssertionError("unexpected X refresh lifecycle request")

    transport = BlockingSecondPageTransport()
    adapter = XRemoteDiscoveryAdapter(transport)
    adapters = Registry(adapter)
    scan_task = None
    monkeypatch.setattr(settings, "remote_discovery_private_members_enabled", True)
    monkeypatch.setattr(settings, "remote_discovery_x_enabled", True)
    try:
        async with async_session() as db:
            await _cleanup(db)
            user = User(
                username=f"{PREFIX}{uuid4().hex[:10]}",
                password_hash="test-only",
                is_active=True,
            )
            db.add(user)
            await db.flush()
            account = await RemoteAccountService(
                db,
                user.id,
                vault=_vault(),
                adapters=adapters,
            ).create(
                {
                    "source": "x",
                    "auth_method": "oauth2",
                    "remote_user_id": "42",
                    "credentials": {
                        "access_token": old_access,
                        "refresh_token": old_refresh,
                        "client_id": client_id,
                    },
                    "collection_selectors": [{"kind": "following"}],
                }
            )
            service = RemoteDiscoveryService(db, vault=_vault(), adapters=adapters)
            task = await service.create_scan(user.id, account.id)
            await db.commit()
            task_id = task.id
            account_id = account.id

        async def run_scan():
            async with async_session() as worker_db:
                return await RemoteDiscoveryService(
                    worker_db,
                    vault=_vault(),
                    adapters=adapters,
                ).run_scan(task_id)

        scan_task = asyncio.create_task(run_scan())
        await asyncio.wait_for(transport.second_started.wait(), timeout=5)
        async with async_session() as observation_db:
            stored_task = await observation_db.get(TaskRun, task_id)
            stored_account = await observation_db.get(RemoteAccount, account_id)
            assert stored_account.credential_generation == 2
            assert stored_task.progress_data["credential_generation"] == 2
            assert stored_account.scan_cursor["credential_generation"] == 2
            assert stored_task.progress_data["remote_identity"] == (
                stored_account.scan_cursor["remote_identity"]
            )
            durable_scan_state = f"{stored_task.progress_data}{stored_account.scan_cursor}"
            if any(secret in durable_scan_state for secret in canaries):
                raise AssertionError("an OAuth credential reached scan progress")

        transport.release_second.set()
        completed = await asyncio.wait_for(scan_task, timeout=10)
        assert completed.status == "complete"
        assert completed.progress_data["credential_generation"] == 2
        if any(secret in str(completed.progress_data) for secret in canaries):
            raise AssertionError("an OAuth credential reached the completed task")
    finally:
        transport.release_second.set()
        if scan_task is not None and not scan_task.done():
            scan_task.cancel()
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalid_grant_durably_marks_only_current_account_and_bindings(
    monkeypatch,
    caplog,
):
    """An invalid refresh token requires reauth without rotating stored material."""

    from app.config import settings
    from app.database import async_session, engine
    from app.models import RemoteAccount, UserSubscriptionSource
    from app.remote_discovery.common import (
        RemoteHTTPResponse,
        RemoteReauthenticationRequired,
    )
    from app.remote_discovery.x import XRemoteDiscoveryAdapter

    old_access = "invalid-old-access-canary"
    old_refresh = "invalid-old-refresh-canary"
    client_id = "invalid-client-canary"
    canaries = (old_access, old_refresh, client_id)
    transport = FixtureTransport(
        RemoteHTTPResponse(401, {"title": "expired"}),
        RemoteHTTPResponse(
            400,
            {"error": "invalid_grant", "error_description": "expired refresh"},
        ),
    )
    adapter = XRemoteDiscoveryAdapter(transport)
    monkeypatch.setattr(settings, "remote_discovery_private_members_enabled", True)
    monkeypatch.setattr(settings, "remote_discovery_x_enabled", True)
    caplog.set_level(logging.DEBUG)
    try:
        async with async_session() as db:
            await _cleanup(db)
            user, account, service = await _create_x_account(
                db,
                adapter,
                suffix="invalid",
                credentials={
                    "access_token": old_access,
                    "refresh_token": old_refresh,
                    "client_id": client_id,
                },
            )
            binding = await _bind_account(db, user, account, suffix="invalid")
            other_user, other_account, _ = await _create_x_account(
                db,
                adapter,
                suffix="invalid_other",
                credentials={
                    "access_token": "other-access",
                    "refresh_token": "other-refresh",
                    "client_id": "other-client",
                },
            )
            other_binding = await _bind_account(
                db,
                other_user,
                other_account,
                suffix="invalid_other",
            )
            await db.commit()

            with pytest.raises(RemoteReauthenticationRequired):
                await service.test(account.id)
            # Mirrors the account-test API exception path.
            await db.commit()

            stored = await db.get(RemoteAccount, account.id, populate_existing=True)
            await db.refresh(binding)
            await db.refresh(other_binding)
            other_stored = await db.get(
                RemoteAccount,
                other_account.id,
                populate_existing=True,
            )
            assert stored.credential_generation == 1
            assert stored.auth_status == "unhealthy"
            assert stored.auth_error_reason == "reauthentication_required"
            assert binding.auth_healthy is False
            assert binding.auth_status == "unhealthy"
            assert binding.auth_error_reason == "reauthentication_required"
            assert other_stored.auth_status == "untested"
            assert other_binding.auth_healthy is True
            assert other_binding.auth_status == "healthy"
            public = await service.get(account.id)
            assert all(secret not in public.model_dump_json() for secret in canaries)
            assert all(secret not in caplog.text for secret in canaries)
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_second_401_keeps_rotation_and_durably_requires_reauthentication(
    monkeypatch,
    caplog,
):
    """The sole retry fails closed after preserving a provider-rotated token pair."""

    from app.config import settings
    from app.database import async_session, engine
    from app.models import RemoteAccount, UserSubscriptionSource
    from app.remote_discovery.common import (
        RemoteHTTPResponse,
        RemoteReauthenticationRequired,
    )
    from app.remote_discovery.x import XRemoteDiscoveryAdapter

    old_access = "retry-old-access-canary"
    old_refresh = "retry-old-refresh-canary"
    rotated_access = "retry-new-access-canary"
    rotated_refresh = "retry-new-refresh-canary"
    client_id = "retry-client-canary"
    canaries = (
        old_access,
        old_refresh,
        rotated_access,
        rotated_refresh,
        client_id,
    )
    transport = FixtureTransport(
        RemoteHTTPResponse(401, {"title": "expired"}),
        RemoteHTTPResponse(
            200,
            {
                "access_token": rotated_access,
                "refresh_token": rotated_refresh,
            },
        ),
        RemoteHTTPResponse(200, {"data": {"id": "42"}}),
        RemoteHTTPResponse(401, {"title": "still unauthorized"}),
    )
    adapter = XRemoteDiscoveryAdapter(transport)
    monkeypatch.setattr(settings, "remote_discovery_private_members_enabled", True)
    monkeypatch.setattr(settings, "remote_discovery_x_enabled", True)
    caplog.set_level(logging.DEBUG)
    try:
        async with async_session() as db:
            await _cleanup(db)
            user, account, service = await _create_x_account(
                db,
                adapter,
                suffix="retry",
                credentials={
                    "access_token": old_access,
                    "refresh_token": old_refresh,
                    "client_id": client_id,
                },
            )
            binding = await _bind_account(db, user, account, suffix="retry")
            await db.commit()

            with pytest.raises(RemoteReauthenticationRequired):
                await service.test(account.id)
            # The coordinator committed generation 2 before this exception;
            # the API's exception commit must persist its health transition too.
            await db.commit()

            stored = await db.get(RemoteAccount, account.id, populate_existing=True)
            decrypted = _vault().decrypt(
                stored.credential_ciphertext,
                user_id=user.id,
                source="x",
                account_id=stored.id,
            ).materialize()
            await db.refresh(binding)
            assert stored.credential_generation == 2
            assert decrypted == {
                "access_token": rotated_access,
                "refresh_token": rotated_refresh,
                "client_id": client_id,
            }
            assert stored.auth_status == "unhealthy"
            assert stored.auth_error_reason == "reauthentication_required"
            assert binding.auth_healthy is False
            assert binding.auth_status == "unhealthy"
            assert binding.auth_error_reason == "reauthentication_required"
            assert sum(url.endswith("/oauth2/token") for _, url, _ in transport.requests) == 1
            assert all(secret not in caplog.text for secret in canaries)
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_sessions_preserve_single_rotated_refresh_token(monkeypatch):
    """Account row serialization prevents two consumers losing a rotated token."""

    import asyncio

    from app.config import settings
    from app.database import async_session, engine
    from app.models import RemoteAccount, UserSubscriptionSource
    from app.remote_discovery.common import RemoteHTTPResponse
    from app.remote_discovery.x import XRemoteDiscoveryAdapter
    from app.services.remote_accounts import RemoteAccountService

    old_access = "concurrent-old-access-canary"
    old_refresh = "concurrent-old-refresh-canary"
    rotated_access = "concurrent-new-access-canary"
    rotated_refresh = "concurrent-new-refresh-canary"
    client_id = "concurrent-client-canary"

    class ConcurrentTransport:
        def __init__(self):
            self.refresh_requests = 0

        async def request(self, method, url, **kwargs):
            if url.endswith("/oauth2/token"):
                self.refresh_requests += 1
                await asyncio.sleep(0)
                return RemoteHTTPResponse(
                    200,
                    {
                        "access_token": rotated_access,
                        "refresh_token": rotated_refresh,
                    },
                )
            if url.endswith("/users/me"):
                authorization = (kwargs.get("headers") or {}).get("Authorization")
                if authorization == f"Bearer {old_access}":
                    return RemoteHTTPResponse(401, {"title": "expired"})
                if authorization != f"Bearer {rotated_access}":
                    raise AssertionError("unexpected access token generation")
                fields = (kwargs.get("params") or {}).get("user.fields")
                if fields == "id":
                    return RemoteHTTPResponse(200, {"data": {"id": "42"}})
                return RemoteHTTPResponse(
                    200,
                    {
                        "data": {
                            "id": "42",
                            "name": "Concurrent Owner",
                            "username": "concurrent_owner",
                        }
                    },
                )
            raise AssertionError("unexpected concurrent X request")

    transport = ConcurrentTransport()
    adapter = XRemoteDiscoveryAdapter(transport)
    adapters = Registry(adapter)
    monkeypatch.setattr(settings, "remote_discovery_private_members_enabled", True)
    monkeypatch.setattr(settings, "remote_discovery_x_enabled", True)
    try:
        async with async_session() as db:
            await _cleanup(db)
            user, account, _ = await _create_x_account(
                db,
                adapter,
                suffix="concurrent",
                credentials={
                    "access_token": old_access,
                    "refresh_token": old_refresh,
                    "client_id": client_id,
                },
            )
            binding = await _bind_account(db, user, account, suffix="concurrent")
            await db.commit()
            user_id = user.id
            account_id = account.id
            binding_id = binding.id

        async def validate_from_independent_session():
            async with async_session() as worker_db:
                result = await RemoteAccountService(
                    worker_db,
                    user_id,
                    vault=_vault(),
                    adapters=adapters,
                ).test(account_id)
                await worker_db.commit()
                return result

        results = await asyncio.gather(
            validate_from_independent_session(),
            validate_from_independent_session(),
        )
        assert [result.auth_status for result in results] == ["healthy", "healthy"]
        assert transport.refresh_requests == 1

        async with async_session() as db:
            stored = await db.get(RemoteAccount, account_id)
            decrypted = _vault().decrypt(
                stored.credential_ciphertext,
                user_id=user_id,
                source="x",
                account_id=account_id,
            ).materialize()
            assert stored.credential_generation == 2
            assert decrypted == {
                "access_token": rotated_access,
                "refresh_token": rotated_refresh,
                "client_id": client_id,
            }
            assert stored.auth_status == "healthy"
            stored_binding = await db.get(UserSubscriptionSource, binding_id)
            assert stored_binding.auth_healthy is True
            assert stored_binding.auth_status == "healthy"
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "outcome", "concurrent_change"),
    (
        ("test", "success", "replace"),
        ("test", "failure", "delete"),
        ("collections", "success", "delete"),
        ("collections", "failure", "replace"),
    ),
)
async def test_post_refresh_provider_window_rejects_concurrent_account_change(
    monkeypatch,
    operation,
    outcome,
    concurrent_change,
):
    """A rotated retry cannot mutate or return across replace/delete."""

    import asyncio

    from app.config import settings
    from app.database import async_session, engine
    from app.models import DiscoveryCandidate, RemoteAccount, UserSubscriptionSource
    from app.remote_discovery.common import RemoteHTTPResponse
    from app.remote_discovery.x import XRemoteDiscoveryAdapter
    from app.services.remote_accounts import (
        RemoteAccountService,
        RemoteCredentialGenerationChanged,
    )

    old_access = f"race-{operation}-{outcome}-old-access"
    old_refresh = f"race-{operation}-{outcome}-old-refresh"
    rotated_access = f"race-{operation}-{outcome}-rotated-access"
    rotated_refresh = f"race-{operation}-{outcome}-rotated-refresh"
    client_id = f"race-{operation}-{outcome}-client"

    class BlockingTransport:
        def __init__(self):
            self.retry_started = asyncio.Event()
            self.release_retry = asyncio.Event()

        async def _finish_retry(self, success_payload):
            self.retry_started.set()
            await self.release_retry.wait()
            if outcome == "failure":
                return RemoteHTTPResponse(401, {"title": "stale unauthorized"})
            return RemoteHTTPResponse(200, success_payload)

        async def request(self, method, url, **kwargs):
            authorization = (kwargs.get("headers") or {}).get("Authorization")
            if url.endswith("/oauth2/token"):
                return RemoteHTTPResponse(
                    200,
                    {
                        "access_token": rotated_access,
                        "refresh_token": rotated_refresh,
                    },
                )
            if url.endswith("/users/me"):
                fields = (kwargs.get("params") or {}).get("user.fields")
                if authorization == f"Bearer {old_access}":
                    return RemoteHTTPResponse(401, {"title": "expired"})
                if authorization != f"Bearer {rotated_access}":
                    raise AssertionError("unexpected access-token generation")
                if fields == "id":
                    return RemoteHTTPResponse(200, {"data": {"id": "42"}})
                return await self._finish_retry(
                    {
                        "data": {
                            "id": "42",
                            "name": "Stale Owner",
                            "username": "stale_owner",
                        }
                    }
                )
            if url.endswith("/users/42/owned_lists"):
                if authorization == f"Bearer {old_access}":
                    return RemoteHTTPResponse(401, {"title": "expired"})
                if authorization != f"Bearer {rotated_access}":
                    raise AssertionError("unexpected access-token generation")
                return await self._finish_retry({"data": [], "meta": {}})
            raise AssertionError(f"unexpected X request: {method} {url}")

    transport = BlockingTransport()
    adapter = XRemoteDiscoveryAdapter(transport)
    adapters = Registry(adapter)
    monkeypatch.setattr(settings, "remote_discovery_private_members_enabled", True)
    monkeypatch.setattr(settings, "remote_discovery_x_enabled", True)
    invocation = None
    try:
        async with async_session() as db:
            await _cleanup(db)
            user, account, _ = await _create_x_account(
                db,
                adapter,
                suffix=f"pw_{operation[:3]}_{outcome[:1]}_{concurrent_change[:1]}",
                credentials={
                    "access_token": old_access,
                    "refresh_token": old_refresh,
                    "client_id": client_id,
                },
            )
            binding = await _bind_account(
                db,
                user,
                account,
                suffix=f"pw_{operation[:3]}_{outcome[:1]}_{concurrent_change[:1]}",
            )
            if concurrent_change == "delete":
                db.add(
                    DiscoveryCandidate(
                        remote_account_id=account.id,
                        user_id=user.id,
                        source_creator_id=f"retained-{uuid4().hex}",
                        state="imported",
                        subscription_id=binding.subscription_id,
                        user_subscription_id=binding.user_subscription_id,
                    )
                )
            await db.commit()
            user_id = user.id
            account_id = account.id
            binding_id = binding.id

        async def invoke_stale_request():
            async with async_session() as request_db:
                service = RemoteAccountService(
                    request_db,
                    user_id,
                    vault=_vault(),
                    adapters=adapters,
                )
                method = getattr(service, operation)
                with pytest.raises(RemoteCredentialGenerationChanged):
                    await method(account_id)
                await request_db.rollback()

        invocation = asyncio.create_task(invoke_stale_request())
        await asyncio.wait_for(transport.retry_started.wait(), timeout=10)

        async with async_session() as concurrent_db:
            service = RemoteAccountService(
                concurrent_db,
                user_id,
                vault=_vault(),
                adapters=adapters,
            )
            if concurrent_change == "replace":
                await service.update(
                    account_id,
                    {
                        "remote_user_id": "84",
                        "credentials": {
                            "access_token": "replacement-access",
                            "refresh_token": "replacement-refresh",
                            "client_id": "replacement-client",
                        },
                    },
                )
            else:
                await service.delete(account_id)
            await concurrent_db.commit()

        transport.release_retry.set()
        await asyncio.wait_for(invocation, timeout=10)

        async with async_session() as db:
            stored = await db.get(RemoteAccount, account_id)
            stored_binding = await db.get(UserSubscriptionSource, binding_id)
            assert stored is not None
            assert stored.credential_generation == 3
            if concurrent_change == "replace":
                assert stored.remote_user_id == "84"
                assert stored.auth_status == "untested"
                assert stored_binding.auth_status == "healthy"
                decrypted = _vault().decrypt(
                    stored.credential_ciphertext,
                    user_id=user_id,
                    source="x",
                    account_id=account_id,
                ).materialize()
                assert decrypted == {
                    "access_token": "replacement-access",
                    "refresh_token": "replacement-refresh",
                    "client_id": "replacement-client",
                }
            else:
                assert stored.auth_status == "deleted"
                assert stored.credential_ciphertext is None
                assert stored_binding.auth_status == "deleted"
                assert stored_binding.auth_error_reason == "Remote account deleted"
    finally:
        transport.release_retry.set()
        if invocation is not None and not invocation.done():
            invocation.cancel()
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("test", "collections"))
async def test_remote_account_api_maps_stale_provider_result_to_conflict(
    monkeypatch,
    operation,
):
    """A superseded provider result is an explicit rollback-only conflict."""

    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.api import remote_accounts as accounts_api
    from app.services.remote_accounts import RemoteCredentialGenerationChanged

    class StaleService:
        def __init__(self, *_args, **_kwargs):
            pass

        async def test(self, _account_id):
            raise RemoteCredentialGenerationChanged("stale provider result")

        async def collections(self, _account_id):
            raise RemoteCredentialGenerationChanged("stale provider result")

    class RecordingDB:
        def __init__(self):
            self.commits = 0
            self.rollbacks = 0

        async def commit(self):
            self.commits += 1

        async def rollback(self):
            self.rollbacks += 1

    monkeypatch.setattr(accounts_api, "RemoteAccountService", StaleService)
    db = RecordingDB()
    endpoint = (
        accounts_api.test_remote_account
        if operation == "test"
        else accounts_api.list_remote_account_collections
    )
    with pytest.raises(HTTPException) as conflict:
        await endpoint(uuid4(), db=db, user=SimpleNamespace(id=1))
    assert conflict.value.status_code == 409
    assert conflict.value.detail == {
        "code": "remote_account_stale",
        "message": "Remote account changed while the provider request was running",
    }
    assert db.rollbacks == 1
    assert db.commits == 0
