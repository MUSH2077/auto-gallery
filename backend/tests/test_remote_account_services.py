"""Remote account lifecycle, adapter boundaries, and X PKCE state safety."""

from __future__ import annotations

import base64
import json
from uuid import uuid4

import pytest


def _vault():
    from app.services.remote_credentials import CredentialVault

    return CredentialVault(base64.urlsafe_b64encode(b"r" * 32).decode())


class FakeAdapter:
    source = "pixiv"

    def __init__(self):
        self.received = []

    async def validate_account(self, credentials):
        from app.remote_discovery.contract import RemoteCandidateIdentity

        materialized = credentials.materialize()
        self.received.append(materialized)
        assert materialized["refresh_token"] == "top-secret-refresh"
        return RemoteCandidateIdentity(
            source="pixiv",
            source_creator_id="4242",
            profile_url="https://www.pixiv.net/users/4242",
            display_name="Remote Artist",
            username="remote_artist",
        )

    async def list_collections(self, credentials):
        from app.remote_discovery.contract import RemoteCollection

        assert credentials.materialize()["refresh_token"] == "top-secret-refresh"
        return (RemoteCollection("public", "Public", {"restrict": "public"}),)


class FakeRegistry:
    def __init__(self, adapter):
        self.adapter = adapter

    def get(self, source):
        assert source == self.adapter.source
        return self.adapter


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remote_account_delete_tombstones_imported_provenance_and_create_revives_it():
    """Delete clears secrets while imported provenance survives a later reconnect."""
    from sqlalchemy import select, text

    from app.database import async_session, engine
    from app.models import (
        Creator,
        DiscoveryCandidate,
        RemoteAccount,
        Subscription,
        SubscriptionSource,
        User,
        UserSubscription,
        UserSubscriptionSource,
    )
    from app.services.remote_accounts import RemoteAccountService

    marker = f"remote_account_test_{uuid4().hex}"
    adapter = FakeAdapter()
    try:
        async with async_session() as db:
            user = User(
                username=marker,
                password_hash="test-only",
                is_active=True,
                permissions=["subscriptions"],
            )
            other = User(
                username=f"{marker}_other",
                password_hash="test-only",
                is_active=True,
                permissions=["subscriptions"],
            )
            db.add_all([user, other])
            await db.flush()
            service = RemoteAccountService(db, user.id, vault=_vault(), adapters=FakeRegistry(adapter))
            account = await service.create(
                {
                    "source": "pixiv",
                    "auth_method": "refresh_token",
                    "credentials": {"refresh_token": "top-secret-refresh"},
                    "collection_selectors": [{"restrict": "public"}],
                }
            )
            await db.commit()

            stored = await db.get(RemoteAccount, account.id)
            assert stored.credential_ciphertext
            assert "top-secret-refresh" not in stored.credential_ciphertext
            public = await service.get(account.id)
            assert public.has_credentials is True
            assert public.credential_mask == {"refresh_token": "••••"}
            dumped = public.model_dump_json()
            assert "top-secret-refresh" not in dumped
            assert "credential_ciphertext" not in dumped
            with pytest.raises(ValueError, match="Remote account not found"):
                await RemoteAccountService(
                    db, other.id, vault=_vault(), adapters=FakeRegistry(adapter)
                ).get(account.id)

            tested = await service.test(account.id)
            assert tested.auth_status == "healthy"
            assert tested.remote_user_id == "4242"
            assert tested.remote_username == "remote_artist"
            collections = await service.collections(account.id)
            assert [item.id for item in collections] == ["public"]

            creator = Creator(name="Imported Account Artist")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="Imported")
            db.add(subscription)
            await db.flush()
            canonical_source = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id="4242",
                source_url="https://www.pixiv.net/users/4242",
            )
            db.add(canonical_source)
            await db.flush()
            member = UserSubscription(
                user_id=user.id,
                subscription_id=subscription.id,
                name="Imported",
            )
            db.add(member)
            await db.flush()
            binding = UserSubscriptionSource(
                user_id=user.id,
                subscription_id=subscription.id,
                user_subscription_id=member.id,
                subscription_source_id=canonical_source.id,
                remote_account_id=account.id,
            )
            pending = DiscoveryCandidate(
                remote_account_id=account.id,
                user_id=user.id,
                source_creator_id="pending",
                state="pending",
            )
            imported = DiscoveryCandidate(
                remote_account_id=account.id,
                user_id=user.id,
                source_creator_id="4242",
                state="imported",
                subscription_id=subscription.id,
                user_subscription_id=member.id,
            )
            db.add_all([binding, pending, imported])
            await db.commit()

            await service.delete(account.id)
            await db.commit()
            tombstone = await db.get(RemoteAccount, account.id)
            assert tombstone is not None
            assert tombstone.auth_status == "deleted"
            assert tombstone.is_enabled is False
            assert tombstone.credential_ciphertext is None
            assert tombstone.credential_metadata is None
            assert tombstone.credential_key_version is None
            assert await db.get(UserSubscription, member.id) is not None
            await db.refresh(binding)
            assert binding.remote_account_id is None
            candidates = (
                await db.execute(
                    select(DiscoveryCandidate).where(DiscoveryCandidate.user_id == user.id)
                )
            ).scalars().all()
            assert [candidate.id for candidate in candidates] == [imported.id]
            assert candidates[0].user_subscription_id == member.id
            assert await service.list() == []
            with pytest.raises(ValueError, match="Remote account not found"):
                await service.get(account.id)

            revived = await service.create(
                {
                    "source": "pixiv",
                    "auth_method": "refresh_token",
                    "credentials": {"refresh_token": "top-secret-refresh"},
                    "collection_selectors": [{"restrict": "private"}],
                }
            )
            await db.commit()
            assert revived.id == account.id
            assert revived.auth_status == "untested"
            assert revived.has_credentials is True
            assert revived.collection_selectors == [{"restrict": "private"}]
            assert [listed.id for listed in await service.list()] == [account.id]
            assert await db.get(DiscoveryCandidate, imported.id) is not None
    finally:
        async with async_session() as db:
            await db.execute(
                text(
                    "DELETE FROM discovery_candidates WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE :marker)"
                ),
                {"marker": f"{marker}%"},
            )
            await db.execute(
                text(
                    "DELETE FROM user_subscription_sources WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE :marker)"
                ),
                {"marker": f"{marker}%"},
            )
            await db.execute(
                text(
                    "DELETE FROM remote_accounts WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE :marker)"
                ),
                {"marker": f"{marker}%"},
            )
            await db.execute(
                text(
                    "DELETE FROM user_subscriptions WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE :marker)"
                ),
                {"marker": f"{marker}%"},
            )
            await db.execute(
                text("DELETE FROM users WHERE username LIKE :marker"),
                {"marker": f"{marker}%"},
            )
            await db.commit()
        await engine.dispose()


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def setex(self, key, ttl, value):
        self.values[key] = value
        self.ttls[key] = ttl

    def getdel(self, key):
        return self.values.pop(key, None)

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        return int(self.values.pop(key, None) is not None)

    def ttl(self, key):
        return self.ttls.get(key, -2)


class FakeOAuthExchange:
    def __init__(self):
        self.calls = []

    async def exchange(self, *, code, verifier, redirect_uri, client_id):
        self.calls.append((code, verifier, redirect_uri, client_id))
        return {
            "access_token": "oauth-access-secret",
            "refresh_token": "oauth-refresh-secret",
            "scope": "users.read follows.read list.read offline.access",
        }


def test_x_pkce_state_is_ten_minute_single_use_and_owner_bound():
    """Redis state contains no tokens and replay/cross-owner callbacks are rejected."""
    from app.services.x_oauth import XOAuthPKCEState

    redis = MemoryRedis()
    state_service = XOAuthPKCEState(
        redis,
        client_id="public-client",
        redirect_uri="https://gallery.example/api/v1/remote-accounts/x/oauth/callback",
    )
    authorization = state_service.authorize(user_id=7)
    assert authorization.state
    assert "code_challenge=" in authorization.url
    assert "users.read" in authorization.url
    key = f"remote-discovery:x:oauth-state:{authorization.state}"
    assert redis.ttls[key] == 600
    stored = redis.values[key].decode() if isinstance(redis.values[key], bytes) else redis.values[key]
    assert "token" not in stored.casefold()

    with pytest.raises(ValueError, match="owner"):
        redis.ttls[key] = 321
        state_service.consume(state=authorization.state, user_id=8)
    # An ownership failure must not burn the rightful user's state.
    assert redis.ttls[key] == 321
    payload = state_service.consume(state=authorization.state, user_id=7)
    assert payload.verifier
    with pytest.raises(ValueError, match="expired or already used"):
        state_service.consume(state=authorization.state, user_id=7)


def test_remote_account_write_schemas_accept_credentials_but_reads_cannot_serialize_them():
    from pydantic import ValidationError

    from app.schemas.remote_discovery import RemoteAccountCreate, RemoteAccountRead

    created = RemoteAccountCreate(
        source="bilibili",
        auth_method="sessdata",
        credentials={"SESSDATA": "secret-cookie"},
    )
    assert created.credentials == {"SESSDATA": "secret-cookie"}
    assert "credentials" not in RemoteAccountRead.model_fields
    assert "credential_ciphertext" not in RemoteAccountRead.model_fields
    assert {"has_credentials", "credential_mask"} <= set(RemoteAccountRead.model_fields)
    with pytest.raises(ValidationError) as invalid:
        RemoteAccountCreate(
            source="pixiv",
            auth_method="cookie",
            credentials={"cookie": "must-not-echo-this-secret"},
        )
    assert "must-not-echo-this-secret" not in str(invalid.value)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remote_account_auth_method_cannot_relabel_existing_ciphertext():
    """Auth method changes require compatible replacement credentials."""
    from sqlalchemy import text

    from app.database import async_session, engine
    from app.models import User
    from app.services.remote_accounts import RemoteAccountService

    marker = f"remote_account_method_{uuid4().hex}"
    try:
        async with async_session() as db:
            user = User(username=marker, password_hash="x", is_active=True)
            db.add(user)
            await db.flush()
            service = RemoteAccountService(db, user.id, vault=_vault(), adapters=FakeRegistry(FakeAdapter()))
            account = await service.create(
                {
                    "source": "x",
                    "auth_method": "oauth2",
                    "credentials": {"access_token": "top-secret-access"},
                }
            )
            with pytest.raises(ValueError, match="replacement credentials"):
                await service.update(account.id, {"auth_method": "cookie"})
            with pytest.raises(ValueError, match="not valid"):
                await service.update(
                    account.id,
                    {"auth_method": "sessdata", "credentials": {"SESSDATA": "secret"}},
                )
            with pytest.raises(ValueError, match="unexpected credential fields"):
                await service.update(
                    account.id,
                    {
                        "credentials": {
                            "access_token": "replacement-access",
                            "a-secret-value-used-as-a-key": "ignored",
                        }
                    },
                )
    finally:
        async with async_session() as db:
            await db.execute(
                text(
                    "DELETE FROM remote_accounts WHERE user_id IN "
                    "(SELECT id FROM users WHERE username=:marker)"
                ),
                {"marker": marker},
            )
            await db.execute(text("DELETE FROM users WHERE username=:marker"), {"marker": marker})
            await db.commit()
        await engine.dispose()


def test_x_oauth_scope_validation_requires_every_discovery_scope():
    from app.services.x_oauth import validate_x_oauth_scopes

    assert validate_x_oauth_scopes(
        "users.read follows.read list.read offline.access"
    ) == ["users.read", "follows.read", "list.read", "offline.access"]
    with pytest.raises(ValueError, match="missing required scopes"):
        validate_x_oauth_scopes("users.read follows.read")


def _headers(username: str) -> dict[str, str]:
    from app.auth import create_access_token

    return {
        "Authorization": f"Bearer {create_access_token(username, must_change_password=False)}"
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remote_account_api_is_owner_scoped_redacted_and_admin_audit_is_metadata_only():
    """Own-account APIs and administrator audit must never serialize credential material."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text

    from app.config import settings
    from app.database import async_session, engine
    from app.main import app
    from app.models import User
    from app.remote_discovery import registry

    marker = f"remote_account_api_{uuid4().hex}"
    old_key = settings.remote_credential_key
    old_adapter = registry._adapters.get("pixiv")
    settings.remote_credential_key = base64.urlsafe_b64encode(b"a" * 32).decode()
    registry.register(FakeAdapter())
    try:
        async with async_session() as db:
            first = User(
                username=marker,
                password_hash="test-only",
                is_active=True,
                permissions=["subscriptions"],
            )
            second = User(
                username=f"{marker}_second",
                password_hash="test-only",
                is_active=True,
                permissions=["subscriptions"],
            )
            admin = User(
                username=f"{marker}_admin",
                password_hash="test-only",
                is_active=True,
                is_admin=True,
            )
            db.add_all([first, second, admin])
            await db.commit()
            first_name, second_name, admin_name = first.username, second.username, admin.username

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/remote-accounts",
                json={
                    "source": "pixiv",
                    "auth_method": "refresh_token",
                    "credentials": {"refresh_token": "top-secret-refresh"},
                },
                headers=_headers(first_name),
            )
            assert response.status_code == 201, response.text
            body = response.json()
            account_id = body["id"]
            assert body["has_credentials"] is True
            assert body["credential_mask"] == {"refresh_token": "••••"}
            assert "top-secret-refresh" not in response.text
            assert "ciphertext" not in response.text

            assert (
                await client.get(f"/api/v1/remote-accounts/{account_id}", headers=_headers(second_name))
            ).status_code == 404
            tested = await client.post(
                f"/api/v1/remote-accounts/{account_id}/test", headers=_headers(first_name)
            )
            assert tested.status_code == 200, tested.text
            assert tested.json()["auth_status"] == "healthy"
            collections = await client.get(
                f"/api/v1/remote-accounts/{account_id}/collections", headers=_headers(first_name)
            )
            assert collections.status_code == 200
            assert collections.json() == [
                {"id": "public", "name": "Public", "selector": {"restrict": "public"}}
            ]

            forbidden = await client.get(
                "/api/v1/admin/audit/remote-accounts", headers=_headers(second_name)
            )
            assert forbidden.status_code == 403
            audit = await client.get(
                "/api/v1/admin/audit/remote-accounts", headers=_headers(admin_name)
            )
            assert audit.status_code == 200, audit.text
            assert audit.json()["total"] == 1
            item = audit.json()["items"][0]
            assert item["has_credentials"] is True
            assert "credential_mask" not in item
            assert "credential" not in json.dumps(item).replace("has_credentials", "")
            assert "top-secret-refresh" not in audit.text
    finally:
        settings.remote_credential_key = old_key
        if old_adapter is not None:
            registry.register(old_adapter)
        async with async_session() as db:
            await db.execute(
                text(
                    "DELETE FROM discovery_candidates WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE :marker)"
                ),
                {"marker": f"{marker}%"},
            )
            await db.execute(
                text(
                    "DELETE FROM user_subscription_sources WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE :marker)"
                ),
                {"marker": f"{marker}%"},
            )
            await db.execute(
                text(
                    "DELETE FROM remote_accounts WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE :marker)"
                ),
                {"marker": f"{marker}%"},
            )
            await db.execute(
                text(
                    "DELETE FROM user_subscriptions WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE :marker)"
                ),
                {"marker": f"{marker}%"},
            )
            await db.execute(
                text("DELETE FROM users WHERE username LIKE :marker"),
                {"marker": f"{marker}%"},
            )
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_x_oauth_api_uses_injected_exchange_and_rejects_replay():
    """OAuth callback stores encrypted tokens only after consuming owner-bound PKCE state."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select, text

    from app.config import settings
    from app.database import async_session, engine
    from app.main import app
    from app.models import RemoteAccount, User
    from app.services.redis_client import get_redis
    from app.services.x_oauth import get_x_oauth_exchange

    marker = f"x_oauth_api_{uuid4().hex}"
    redis = MemoryRedis()
    exchange = FakeOAuthExchange()
    old_key = settings.remote_credential_key
    old_client_id = settings.x_oauth_client_id
    old_redirect = settings.x_oauth_redirect_uri
    settings.remote_credential_key = base64.urlsafe_b64encode(b"o" * 32).decode()
    settings.x_oauth_client_id = "test-public-client"
    settings.x_oauth_redirect_uri = "https://gallery.example/api/v1/remote-accounts/x/oauth/callback"
    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[get_x_oauth_exchange] = lambda: exchange
    try:
        async with async_session() as db:
            user = User(
                username=marker,
                password_hash="test-only",
                is_active=True,
                permissions=["subscriptions"],
            )
            db.add(user)
            await db.commit()
            username = user.username
            user_id = user.id

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            missing_account = await client.get(
                "/api/v1/remote-accounts/x/oauth/authorize",
                params={"account_id": str(uuid4())},
                headers=_headers(username),
            )
            assert missing_account.status_code == 404
            authorize = await client.get(
                "/api/v1/remote-accounts/x/oauth/authorize", headers=_headers(username)
            )
            assert authorize.status_code == 200, authorize.text
            state = authorize.json()["state"]
            callback = await client.get(
                "/api/v1/remote-accounts/x/oauth/callback",
                params={"state": state, "code": "authorization-code"},
                headers=_headers(username),
            )
            assert callback.status_code == 200, callback.text
            assert callback.json()["source"] == "x"
            assert callback.json()["has_credentials"] is True
            assert "oauth-access-secret" not in callback.text
            replay = await client.get(
                "/api/v1/remote-accounts/x/oauth/callback",
                params={"state": state, "code": "authorization-code"},
                headers=_headers(username),
            )
            assert replay.status_code == 400

        async with async_session() as db:
            account = (
                await db.execute(
                    select(RemoteAccount).where(RemoteAccount.user_id == user_id)
                )
            ).scalar_one()
            assert "oauth-access-secret" not in account.credential_ciphertext
            assert not any("oauth-access-secret" in str(value) for value in redis.values.values())
    finally:
        app.dependency_overrides.pop(get_redis, None)
        app.dependency_overrides.pop(get_x_oauth_exchange, None)
        settings.remote_credential_key = old_key
        settings.x_oauth_client_id = old_client_id
        settings.x_oauth_redirect_uri = old_redirect
        async with async_session() as db:
            await db.execute(
                text(
                    "DELETE FROM remote_accounts WHERE user_id IN "
                    "(SELECT id FROM users WHERE username LIKE :marker)"
                ),
                {"marker": f"{marker}%"},
            )
            await db.execute(
                text("DELETE FROM users WHERE username LIKE :marker"),
                {"marker": f"{marker}%"},
            )
            await db.commit()
        await engine.dispose()
