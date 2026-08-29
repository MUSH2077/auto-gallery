"""Credential-scoped live work-state service behavior."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.remote_discovery.common import MalformedRemoteResponse, RemoteRateLimited


PREFIX = "remote_work_state_"
TEST_REMOTE_KEY = base64.urlsafe_b64encode(b"w" * 32).decode()


class Registry:
    def __init__(self, adapter):
        self.adapter = adapter

    def get(self, source):
        assert source == "pixiv"
        return self.adapter


class RecordingWorkStateAdapter:
    source = "pixiv"

    def __init__(self, *, outcome=None):
        self.outcome = outcome
        self.tokens: list[str] = []

    async def fetch_work_state(self, credentials, *, source_work_id):
        values = credentials.materialize()
        self.tokens.append(values["refresh_token"])
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if callable(self.outcome):
            return await self.outcome(credentials, source_work_id)
        from app.remote_discovery.contract import RemoteWorkState

        return RemoteWorkState(
            source="pixiv",
            source_work_id=source_work_id,
            fetched_at=datetime.now(timezone.utc),
            total_views=11,
            total_bookmarks=7,
            is_bookmarked=True,
        )


class BlockingWorkStateAdapter(RecordingWorkStateAdapter):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def fetch_work_state(self, credentials, *, source_work_id):
        values = credentials.materialize()
        self.tokens.append(values["refresh_token"])
        self.started.set()
        await self.release.wait()
        from app.remote_discovery.contract import RemoteWorkState

        return RemoteWorkState(
            source="pixiv",
            source_work_id=source_work_id,
            fetched_at=datetime.now(timezone.utc),
            total_views=11,
            total_bookmarks=7,
            is_bookmarked=True,
        )


class ApiWorkStateAdapter:
    """Injected boundary for route tests; it never performs network I/O."""

    source = "pixiv"

    def __init__(self, outcome=None):
        self.outcome = outcome
        self.calls = 0

    async def fetch_work_state(self, _credentials, *, source_work_id):
        from app.remote_discovery.contract import RemoteWorkState

        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return RemoteWorkState(
            source="pixiv",
            source_work_id=source_work_id,
            fetched_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
            total_views=321,
            total_bookmarks=45,
            is_bookmarked=True,
        )


async def seed_user(db, suffix):
    from app.models import User

    user = User(
        username=f"{PREFIX}{suffix}_{uuid4().hex[:8]}",
        password_hash="test-only",
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


async def seed_pixiv_account(
    db,
    user_id,
    vault,
    *,
    token="owner-token",
    auth_status="healthy",
    is_enabled=True,
    ciphertext=True,
):
    from app.models import RemoteAccount

    account = RemoteAccount(
        user_id=user_id,
        source="pixiv",
        auth_method="refresh_token",
        auth_status=auth_status,
        is_enabled=is_enabled,
        credential_generation=1,
    )
    db.add(account)
    await db.flush()
    if ciphertext:
        account.credential_ciphertext = vault.encrypt(
            {"refresh_token": token},
            user_id=user_id,
            source="pixiv",
            account_id=account.id,
        )
        account.credential_key_version = 1
        account.credential_metadata = {"fields": ["refresh_token"]}
    await db.flush()
    return account


async def seed_binding(db, user, account, suffix):
    from app.models import (
        Creator,
        Subscription,
        SubscriptionSource,
        UserSubscription,
        UserSubscriptionSource,
    )

    creator = Creator(name=f"{PREFIX}{suffix}_{uuid4().hex[:8]}")
    db.add(creator)
    await db.flush()
    subscription = Subscription(creator_id=creator.id, name=creator.name)
    db.add(subscription)
    await db.flush()
    member = UserSubscription(
        user_id=user.id,
        subscription_id=subscription.id,
        name=creator.name,
    )
    db.add(member)
    await db.flush()
    source = SubscriptionSource(
        subscription_id=subscription.id,
        source="pixiv",
        source_creator_id=f"{PREFIX}{suffix}",
        source_url="https://www.pixiv.net/users/1",
    )
    db.add(source)
    await db.flush()
    binding = UserSubscriptionSource(
        user_id=user.id,
        subscription_id=subscription.id,
        user_subscription_id=member.id,
        subscription_source_id=source.id,
        remote_account_id=account.id,
        is_enabled=True,
        auth_healthy=True,
        auth_status="healthy",
    )
    db.add(binding)
    await db.flush()
    return binding


async def cleanup(db):
    params = {"prefix": f"{PREFIX}%"}
    for table in (
        "discovery_candidates",
        "user_subscription_sources",
        "remote_accounts",
        "user_subscriptions",
    ):
        await db.execute(
            text(
                f"DELETE FROM {table} WHERE user_id IN "
                "(SELECT id FROM users WHERE username LIKE :prefix)"
            ),
            params,
        )
    await db.execute(
        text(
            "DELETE FROM subscription_sources WHERE subscription_id IN ("
            "SELECT s.id FROM subscriptions s JOIN creators c ON c.id = s.creator_id "
            "WHERE c.name LIKE :prefix)"
        ),
        params,
    )
    await db.execute(
        text(
            "DELETE FROM subscriptions WHERE creator_id IN "
            "(SELECT id FROM creators WHERE name LIKE :prefix)"
        ),
        params,
    )
    await db.execute(text("DELETE FROM creators WHERE name LIKE :prefix"), params)
    await db.execute(text("DELETE FROM users WHERE username LIKE :prefix"), params)
    await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_work_state_uses_only_current_users_enabled_healthy_pixiv_account():
    from app.database import async_session, engine
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_credentials import CredentialVault

    adapter = RecordingWorkStateAdapter()
    try:
        async with async_session() as db:
            await cleanup(db)
            vault = CredentialVault(TEST_REMOTE_KEY)
            owner = await seed_user(db, "owner")
            other = await seed_user(db, "other")
            owner_account = await seed_pixiv_account(db, owner.id, vault)
            await seed_pixiv_account(db, other.id, vault, token="other-token")

            state = await RemoteAccountService(
                db, owner.id, vault=vault, adapters=Registry(adapter)
            ).fetch_work_state("pixiv", "38362603")

            assert state.source_work_id == "38362603"
            assert (state.total_views, state.total_bookmarks, state.is_bookmarked) == (11, 7, True)
            assert adapter.tokens == ["owner-token"]
            assert owner_account.auth_status == "healthy"
    finally:
        async with async_session() as db:
            await cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account_kwargs", "exception_name"),
    [
        (None, "RemoteWorkStateAccountRequired"),
        ({"is_enabled": False}, "RemoteWorkStateAccountRequired"),
        ({"auth_status": None}, "RemoteWorkStateAccountUnhealthy"),
        ({"auth_status": "untested"}, "RemoteWorkStateAccountUnhealthy"),
        ({"auth_status": "unhealthy"}, "RemoteWorkStateAccountUnhealthy"),
        ({"auth_status": "deleted"}, "RemoteWorkStateAccountRequired"),
        ({"ciphertext": False}, "RemoteWorkStateAccountRequired"),
    ],
)
async def test_work_state_rejects_missing_or_unusable_current_user_account(
    account_kwargs, exception_name
):
    from app.database import async_session, engine
    from app.services import remote_accounts
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_credentials import CredentialVault

    adapter = RecordingWorkStateAdapter()
    try:
        async with async_session() as db:
            await cleanup(db)
            vault = CredentialVault(TEST_REMOTE_KEY)
            owner = await seed_user(db, "unusable")
            if account_kwargs is not None:
                await seed_pixiv_account(db, owner.id, vault, **account_kwargs)

            with pytest.raises(getattr(remote_accounts, exception_name)):
                await RemoteAccountService(
                    db, owner.id, vault=vault, adapters=Registry(adapter)
                ).fetch_work_state("pixiv", "38362603")

            assert adapter.tokens == []
    finally:
        async with async_session() as db:
            await cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_work_state_does_not_borrow_another_users_account():
    from app.database import async_session, engine
    from app.services.remote_accounts import (
        RemoteAccountService,
        RemoteWorkStateAccountRequired,
    )
    from app.services.remote_credentials import CredentialVault

    adapter = RecordingWorkStateAdapter()
    try:
        async with async_session() as db:
            await cleanup(db)
            vault = CredentialVault(TEST_REMOTE_KEY)
            owner = await seed_user(db, "accountless")
            other = await seed_user(db, "other_only")
            await seed_pixiv_account(db, other.id, vault, token="other-token")

            with pytest.raises(RemoteWorkStateAccountRequired):
                await RemoteAccountService(
                    db, owner.id, vault=vault, adapters=Registry(adapter)
                ).fetch_work_state("pixiv", "38362603")

            assert adapter.tokens == []
    finally:
        async with async_session() as db:
            await cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome_factory", "exception_type"),
    [
        (lambda: RemoteRateLimited(30), RemoteRateLimited),
        (lambda: TimeoutError("provider timeout"), TimeoutError),
        (lambda: MalformedRemoteResponse("malformed provider response"), MalformedRemoteResponse),
        (lambda: RuntimeError("provider failure"), RuntimeError),
    ],
)
async def test_work_state_non_reauthentication_provider_failures_do_not_mutate_account(
    outcome_factory, exception_type
):
    from app.database import async_session, engine
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_credentials import CredentialVault

    adapter = RecordingWorkStateAdapter(outcome=outcome_factory())
    try:
        async with async_session() as db:
            await cleanup(db)
            vault = CredentialVault(TEST_REMOTE_KEY)
            owner = await seed_user(db, "provider_failure")
            account = await seed_pixiv_account(db, owner.id, vault)
            original = (
                account.auth_status,
                account.auth_error_reason,
                account.credential_generation,
            )

            with pytest.raises(exception_type):
                await RemoteAccountService(
                    db, owner.id, vault=vault, adapters=Registry(adapter)
                ).fetch_work_state("pixiv", "38362603")

            assert adapter.tokens == ["owner-token"]
            assert (
                account.auth_status,
                account.auth_error_reason,
                account.credential_generation,
            ) == original
    finally:
        async with async_session() as db:
            await cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returned_source", "returned_work_id"),
    [
        ("x", "38362603"),
        ("pixiv", "different-work-id"),
    ],
)
async def test_work_state_rejects_mismatched_adapter_identity_without_mutating_account(
    returned_source, returned_work_id
):
    from app.database import async_session, engine
    from app.remote_discovery.contract import RemoteWorkState
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_credentials import CredentialVault

    async def mismatched_state(_credentials, _source_work_id):
        return RemoteWorkState(
            source=returned_source,
            source_work_id=returned_work_id,
            fetched_at=datetime.now(timezone.utc),
            total_views=11,
            total_bookmarks=7,
            is_bookmarked=True,
        )

    adapter = RecordingWorkStateAdapter(outcome=mismatched_state)
    try:
        async with async_session() as db:
            await cleanup(db)
            vault = CredentialVault(TEST_REMOTE_KEY)
            owner = await seed_user(db, "mismatched_identity")
            account = await seed_pixiv_account(db, owner.id, vault)
            original = (
                account.auth_status,
                account.auth_error_reason,
                account.credential_generation,
            )

            with pytest.raises(ValueError, match="different work"):
                await RemoteAccountService(
                    db, owner.id, vault=vault, adapters=Registry(adapter)
                ).fetch_work_state("pixiv", "38362603")

            assert adapter.tokens == ["owner-token"]
            assert (
                account.auth_status,
                account.auth_error_reason,
                account.credential_generation,
            ) == original
    finally:
        async with async_session() as db:
            await cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_work_state_rejects_a_result_after_credential_generation_changes():
    """A result fetched with a stale credential generation is never returned."""

    from app.database import async_session, engine
    from app.models import RemoteAccount
    from app.services.remote_accounts import (
        RemoteAccountService,
        RemoteCredentialGenerationChanged,
    )
    from app.services.remote_credentials import CredentialVault

    adapter = BlockingWorkStateAdapter()
    invocation = None
    try:
        async with async_session() as db:
            await cleanup(db)
            vault = CredentialVault(TEST_REMOTE_KEY)
            owner = await seed_user(db, "generation")
            account = await seed_pixiv_account(db, owner.id, vault)
            await db.commit()
            owner_id, account_id = owner.id, account.id

        async def invoke():
            async with async_session() as db:
                with pytest.raises(RemoteCredentialGenerationChanged):
                    await RemoteAccountService(
                        db,
                        owner_id,
                        vault=CredentialVault(TEST_REMOTE_KEY),
                        adapters=Registry(adapter),
                    ).fetch_work_state("pixiv", "38362603")
                await db.rollback()

        invocation = asyncio.create_task(invoke())
        await asyncio.wait_for(adapter.started.wait(), timeout=5)
        async with async_session() as replacement_db:
            account = await replacement_db.get(RemoteAccount, account_id)
            account.credential_generation += 1
            await replacement_db.commit()
        adapter.release.set()
        await asyncio.wait_for(invocation, timeout=10)
        assert adapter.tokens == ["owner-token"]
    finally:
        adapter.release.set()
        if invocation is not None and not invocation.done():
            invocation.cancel()
        async with async_session() as db:
            await cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_work_state_reauthentication_marks_only_selected_account_and_bindings_unhealthy():
    from app.database import async_session, engine
    from app.models import RemoteAccount, UserSubscriptionSource
    from app.remote_discovery.common import RemoteReauthenticationRequired
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_credentials import CredentialVault

    canary = "owner-token-canary"
    adapter = RecordingWorkStateAdapter(outcome=RemoteReauthenticationRequired(401))
    try:
        async with async_session() as db:
            await cleanup(db)
            vault = CredentialVault(TEST_REMOTE_KEY)
            owner = await seed_user(db, "reauth_owner")
            other = await seed_user(db, "reauth_other")
            account = await seed_pixiv_account(db, owner.id, vault, token=canary)
            other_account = await seed_pixiv_account(db, other.id, vault, token="other-token")
            binding = await seed_binding(db, owner, account, "reauth")
            await db.commit()
            account_id, other_account_id, binding_id = account.id, other_account.id, binding.id

            with pytest.raises(RemoteReauthenticationRequired):
                await RemoteAccountService(
                    db, owner.id, vault=vault, adapters=Registry(adapter)
                ).fetch_work_state("pixiv", "38362603")
            await db.commit()

            selected = await db.get(RemoteAccount, account_id)
            other_selected = await db.get(RemoteAccount, other_account_id)
            updated_binding = await db.get(UserSubscriptionSource, binding_id)
            assert selected.auth_status == "unhealthy"
            assert selected.auth_error_reason == "reauthentication_required"
            assert updated_binding.auth_healthy is False
            assert updated_binding.auth_status == "unhealthy"
            assert updated_binding.auth_error_reason == "reauthentication_required"
            assert other_selected.auth_status == "healthy"
            assert canary not in (selected.auth_error_reason or "")
            assert canary not in (updated_binding.auth_error_reason or "")
    finally:
        async with async_session() as db:
            await cleanup(db)
        await engine.dispose()


async def _cleanup_api_work_state(db):
    await db.execute(
        text("DELETE FROM work_sources WHERE source_work_id = '38362603'")
    )
    await db.execute(text("DELETE FROM works WHERE title LIKE :prefix"), {"prefix": f"{PREFIX}api%"})
    await cleanup(db)


async def _seed_api_work_state(db, *, source="pixiv", is_nsfw=False):
    from app.models import Work, WorkSource

    work = Work(title=f"{PREFIX}api_{uuid4().hex}", is_nsfw=is_nsfw)
    db.add(work)
    await db.flush()
    source_row = WorkSource(
        work_id=work.id,
        source=source,
        source_work_id="38362603",
        raw_metadata={"total_view": 3, "total_bookmarks": 4, "provider_payload": "remote-payload-canary"},
    )
    db.add(source_row)
    await db.commit()
    await db.refresh(work)
    return work, source_row


def _api_headers(username):
    from app.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token(username, must_change_password=False)}"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remote_work_state_api_returns_live_pixiv_state_without_metadata_fallback(monkeypatch):
    """Every endpoint request uses the injected live adapter, never stale metadata."""
    from app.config import settings
    from app.database import async_session, engine
    from app.main import app
    from app.remote_discovery import registry
    from app.services.remote_credentials import CredentialVault

    adapter = ApiWorkStateAdapter()
    old_key = settings.remote_credential_key
    old_adapter = registry._adapters.get("pixiv")
    settings.remote_credential_key = TEST_REMOTE_KEY
    registry.register(adapter)
    try:
        async with async_session() as db:
            await _cleanup_api_work_state(db)
            user = await seed_user(db, "api_success")
            user.permissions = ["library"]
            await db.flush()
            await seed_pixiv_account(db, user.id, CredentialVault(TEST_REMOTE_KEY))
            work, work_source = await _seed_api_work_state(db)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/api/v1/works/{work.id}/remote-state", headers=_api_headers(user.username)
            )
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "private, no-store"
            assert response.json() == {
                "source": "pixiv",
                "source_work_id": "38362603",
                "fetched_at": "2026-08-30T00:00:00Z",
                "total_views": 321,
                "total_bookmarks": 45,
                "is_bookmarked": True,
            }
            assert response.json()["total_views"] != work_source.raw_metadata["total_view"]
            assert response.json()["total_bookmarks"] != work_source.raw_metadata["total_bookmarks"]

            second = await client.get(
                f"/api/v1/works/{work.id}/remote-state", headers=_api_headers(user.username)
            )
            assert second.status_code == 200, second.text
            assert adapter.calls == 2
    finally:
        settings.remote_credential_key = old_key
        if old_adapter is None:
            registry._adapters.pop("pixiv", None)
        else:
            registry.register(old_adapter)
        async with async_session() as db:
            await _cleanup_api_work_state(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_code", "retry_after"),
    [
        (RemoteRateLimited(-7), 429, "remote_provider_rate_limited", "1"),
        (MalformedRemoteResponse("remote-payload-canary"), 502, "remote_provider_unavailable", None),
        (TimeoutError("owner-token-canary"), 502, "remote_provider_unavailable", None),
    ],
)
async def test_remote_work_state_api_sanitizes_provider_failures(
    monkeypatch, caplog, outcome, expected_status, expected_code, retry_after
):
    from app.config import settings
    from app.database import async_session, engine
    from app.main import app
    from app.remote_discovery import registry
    from app.services.remote_credentials import CredentialVault

    adapter = ApiWorkStateAdapter(outcome)
    old_key = settings.remote_credential_key
    old_adapter = registry._adapters.get("pixiv")
    settings.remote_credential_key = TEST_REMOTE_KEY
    registry.register(adapter)
    try:
        async with async_session() as db:
            await _cleanup_api_work_state(db)
            user = await seed_user(db, "api_provider_failure")
            user.permissions = ["library"]
            await db.flush()
            await seed_pixiv_account(db, user.id, CredentialVault(TEST_REMOTE_KEY), token="owner-token-canary")
            work, _ = await _seed_api_work_state(db)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/api/v1/works/{work.id}/remote-state", headers=_api_headers(user.username)
            )
        assert response.status_code == expected_status, response.text
        assert response.json()["detail"]["code"] == expected_code
        if retry_after is None:
            assert "retry-after" not in response.headers
        else:
            assert response.headers["retry-after"] == retry_after
        assert "owner-token-canary" not in response.text
        assert "remote-payload-canary" not in response.text
        assert "owner-token-canary" not in caplog.text
        assert "remote-payload-canary" not in caplog.text
    finally:
        settings.remote_credential_key = old_key
        if old_adapter is None:
            registry._adapters.pop("pixiv", None)
        else:
            registry.register(old_adapter)
        async with async_session() as db:
            await _cleanup_api_work_state(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remote_work_state_api_maps_account_and_rollout_failures(monkeypatch, caplog):
    from app.config import settings
    from app.database import async_session, engine
    from app.main import app
    from app.remote_discovery import registry
    from app.remote_discovery.common import RemoteReauthenticationRequired
    from app.services.remote_credentials import CredentialVault

    token_canary = "owner-token-canary"
    payload_canary = "remote-payload-canary"
    old_key = settings.remote_credential_key
    old_adapter = registry._adapters.get("pixiv")
    old_private_members = settings.remote_discovery_private_members_enabled
    settings.remote_credential_key = TEST_REMOTE_KEY
    try:
        async with async_session() as db:
            await _cleanup_api_work_state(db)
            required = await seed_user(db, "api_required")
            unhealthy = await seed_user(db, "api_unhealthy")
            reauth = await seed_user(db, "api_reauth")
            stale = await seed_user(db, "api_stale")
            for user in (required, unhealthy, reauth, stale):
                user.permissions = ["library"]
            await db.flush()
            vault = CredentialVault(TEST_REMOTE_KEY)
            await seed_pixiv_account(db, unhealthy.id, vault, auth_status="unhealthy")
            reauth_account = await seed_pixiv_account(db, reauth.id, vault)
            await seed_pixiv_account(db, stale.id, vault)
            work, _ = await _seed_api_work_state(db)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            required_response = await client.get(
                f"/api/v1/works/{work.id}/remote-state", headers=_api_headers(required.username)
            )
            assert required_response.status_code == 409
            assert required_response.json()["detail"]["code"] == "remote_account_required"

            unhealthy_response = await client.get(
                f"/api/v1/works/{work.id}/remote-state", headers=_api_headers(unhealthy.username)
            )
            assert unhealthy_response.status_code == 409
            assert unhealthy_response.json()["detail"]["code"] == "remote_account_reauthentication_required"

            registry.register(
                ApiWorkStateAdapter(
                    RemoteReauthenticationRequired(401, f"{token_canary} {payload_canary}")
                )
            )
            reauth_response = await client.get(
                f"/api/v1/works/{work.id}/remote-state", headers=_api_headers(reauth.username)
            )
            assert reauth_response.status_code == 409
            assert reauth_response.json()["detail"]["code"] == "remote_account_reauthentication_required"
            assert token_canary not in reauth_response.text
            assert payload_canary not in reauth_response.text

            from app.services.remote_accounts import RemoteCredentialGenerationChanged
            registry.register(
                ApiWorkStateAdapter(
                    RemoteCredentialGenerationChanged(f"{token_canary} {payload_canary}")
                )
            )
            stale_response = await client.get(
                f"/api/v1/works/{work.id}/remote-state", headers=_api_headers(stale.username)
            )
            assert stale_response.status_code == 409
            assert stale_response.json()["detail"]["code"] == "remote_account_stale"
            assert token_canary not in stale_response.text
            assert payload_canary not in stale_response.text
            assert token_canary not in caplog.text
            assert payload_canary not in caplog.text

            settings.remote_discovery_private_members_enabled = False
            unavailable = await client.get(
                f"/api/v1/works/{work.id}/remote-state", headers=_api_headers(stale.username)
            )
            assert unavailable.status_code == 503
            assert unavailable.json()["detail"]["code"] == "remote_discovery_unavailable"

        async with async_session() as db:
            from app.models import RemoteAccount

            stored_reauth_account = await db.get(RemoteAccount, reauth_account.id)
            assert stored_reauth_account.auth_status == "unhealthy"
            assert stored_reauth_account.auth_error_reason == "reauthentication_required"
    finally:
        settings.remote_credential_key = old_key
        settings.remote_discovery_private_members_enabled = old_private_members
        if old_adapter is None:
            registry._adapters.pop("pixiv", None)
        else:
            registry.register(old_adapter)
        async with async_session() as db:
            await _cleanup_api_work_state(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remote_work_state_api_rejects_missing_and_non_pixiv_works_before_provider(monkeypatch):
    from app.config import settings
    from app.database import async_session, engine
    from app.main import app
    from app.remote_discovery import registry

    adapter = ApiWorkStateAdapter()
    old_adapter = registry._adapters.get("pixiv")
    registry.register(adapter)
    try:
        async with async_session() as db:
            await _cleanup_api_work_state(db)
            user = await seed_user(db, "api_unsupported")
            user.permissions = ["library"]
            await db.flush()
            work, _ = await _seed_api_work_state(db, source="manual")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            unsupported = await client.get(
                f"/api/v1/works/{work.id}/remote-state", headers=_api_headers(user.username)
            )
            assert unsupported.status_code == 409
            assert unsupported.json()["detail"]["code"] == "remote_work_state_unsupported"

            missing = await client.get(
                f"/api/v1/works/{uuid4()}/remote-state", headers=_api_headers(user.username)
            )
            assert missing.status_code == 404
            assert adapter.calls == 0
    finally:
        if old_adapter is None:
            registry._adapters.pop("pixiv", None)
        else:
            registry.register(old_adapter)
        async with async_session() as db:
            await _cleanup_api_work_state(db)
        await engine.dispose()
