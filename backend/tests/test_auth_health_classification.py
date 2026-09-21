from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _policy(
    *,
    enabled: bool = True,
    auth_healthy: bool = True,
    auth_status: str | None = None,
    auth_error_reason: str | None = None,
    checked_at: datetime | None = None,
    remote_account_id=None,
):
    return SimpleNamespace(
        is_enabled=enabled,
        auth_healthy=auth_healthy,
        auth_status=auth_status,
        auth_error_reason=auth_error_reason,
        last_auth_checked_at=checked_at,
        remote_account_id=remote_account_id,
    )


def _subscription(*, active: bool = True, sync_enabled: bool = True):
    return SimpleNamespace(is_active=active, sync_enabled=sync_enabled)


@pytest.mark.parametrize(
    ("policy", "subscription", "auth_state", "actionable", "deferred"),
    [
        (_policy(enabled=False, auth_healthy=False), _subscription(), "unknown", False, True),
        (
            _policy(
                auth_healthy=False,
                auth_status="unhealthy",
                auth_error_reason="cookie expired",
            ),
            _subscription(active=False),
            "unhealthy",
            False,
            True,
        ),
        (
            _policy(auth_healthy=False, auth_status="failed", checked_at=NOW),
            _subscription(sync_enabled=False),
            "unhealthy",
            False,
            True,
        ),
        (_policy(auth_healthy=False), _subscription(), "unknown", False, True),
        (
            _policy(auth_healthy=False, auth_status="unhealthy", checked_at=NOW),
            _subscription(),
            "unhealthy",
            True,
            False,
        ),
        (
            _policy(auth_healthy=False, auth_status="unhealthy"),
            _subscription(),
            "unknown",
            False,
            True,
        ),
        (
            _policy(auth_status="healthy", checked_at=NOW),
            _subscription(),
            "healthy",
            False,
            False,
        ),
    ],
)
def test_auth_health_requires_real_failure_evidence_and_active_sync(
    policy,
    subscription,
    auth_state,
    actionable,
    deferred,
):
    from app.services.auth_health import classify_source_health

    result = classify_source_health(policy, subscription)

    assert result.auth_state == auth_state
    assert result.actionable is actionable
    assert result.disabled_or_unchecked is deferred


def test_credential_state_is_independent_from_auth_attempt_state():
    from app.services.auth_health import classify_source_health

    account_id = uuid4()
    failed_policy = _policy(
        auth_healthy=False,
        auth_status="unhealthy",
        checked_at=NOW,
        remote_account_id=account_id,
    )
    ready = classify_source_health(
        failed_policy,
        _subscription(),
        remote_account=SimpleNamespace(
            id=account_id,
            is_enabled=True,
            credential_ciphertext="encrypted",
        ),
    )
    missing = classify_source_health(
        _policy(remote_account_id=account_id, auth_status="healthy", checked_at=NOW),
        _subscription(),
        remote_account=None,
    )
    public = classify_source_health(_policy(), _subscription())

    assert (ready.auth_state, ready.credential_state) == ("unhealthy", "ready")
    assert (missing.auth_state, missing.credential_state) == ("healthy", "missing")
    assert missing.credential_issue is True
    assert public.credential_state == "not_required"


def test_membership_usability_does_not_treat_legacy_false_as_auth_evidence():
    from app.services.subscription_membership import membership_source_is_usable

    unchecked = _policy(auth_healthy=False)
    assert membership_source_is_usable(unchecked, None, source="pixiv") is True

    failed = _policy(auth_healthy=False, auth_status="unhealthy", checked_at=NOW)
    assert membership_source_is_usable(failed, None, source="pixiv") is False


def test_destination_credentials_do_not_overwrite_attempt_health():
    from app.services.subscription_membership import reset_binding_auth_for_destination

    account_id = uuid4()
    binding = _policy(auth_healthy=False, remote_account_id=account_id)
    account = SimpleNamespace(
        id=account_id,
        is_enabled=False,
        credential_ciphertext=None,
        auth_status="healthy",
        auth_error_reason=None,
        last_authenticated_at=NOW,
    )

    reset_binding_auth_for_destination(binding, account)

    assert binding.auth_healthy is True
    assert binding.auth_status == "healthy"
    assert binding.last_auth_checked_at == NOW


def test_repository_search_filters_use_attempt_state_not_legacy_boolean():
    from app.services.search import INDEX_SETTINGS, IS_FIELD, REPOSITORIES_INDEX

    assert IS_FIELD["repositories"]["auth-ok"] == ("auth_state", "healthy")
    assert IS_FIELD["repositories"]["auth-error"] == (
        "auth_state",
        "unhealthy",
    )
    assert "auth_state" in INDEX_SETTINGS[REPOSITORIES_INDEX]["filterableAttributes"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_auth_attention_counts_exclude_disabled_inactive_and_unchecked_sources():
    from app.database import async_session, engine
    from app.models import Creator, Subscription, SubscriptionSource
    from app.services.auth_health import auth_attention_counts

    try:
        async with async_session() as db:
            await db.execute(
                text(
                    "TRUNCATE user_subscription_sources, user_subscriptions, "
                    "subscription_sources, subscriptions, creators RESTART IDENTITY CASCADE"
                )
            )
            def add_source(
                name: str,
                *,
                source_enabled: bool = True,
                active: bool = True,
                sync_enabled: bool = True,
                status: str | None = None,
                error: str | None = None,
                checked_at: datetime | None = None,
            ) -> None:
                creator = Creator(name=f"auth-counts-{name}")
                db.add(creator)
                subscription = Subscription(
                    creator=creator,
                    name=name,
                    is_active=active,
                    sync_enabled=sync_enabled,
                    schedule_mode=None if sync_enabled else "manual",
                )
                db.add(subscription)
                source = SubscriptionSource(
                    subscription=subscription,
                    source="pixiv",
                    source_creator_id=name,
                    source_url=f"https://www.pixiv.net/users/{name}",
                    is_enabled=source_enabled,
                    auth_healthy=status not in {"failed", "unhealthy"},
                    auth_status=status,
                    auth_error_reason=error,
                    last_auth_checked_at=checked_at,
                )
                db.add(source)

            add_source("disabled", source_enabled=False)
            add_source("inactive", active=False, status="unhealthy", checked_at=NOW)
            add_source("sync-off", sync_enabled=False, status="failed", error="expired")
            add_source("unchecked")
            add_source("actionable", status="unhealthy", checked_at=NOW)
            add_source("healthy", status="healthy", checked_at=NOW)
            await db.commit()

            counts = await auth_attention_counts(db)

            assert counts == {
                "auth_actionable_count": 1,
                "auth_disabled_or_unchecked_count": 4,
                "credential_issue_count": 0,
            }

            from app.api.admin.auth_health import get_auth_status

            status = await get_auth_status(db)
            by_name = {
                source["source_creator_id"]: source
                for source in status["sources"]
            }
            assert by_name["unchecked"]["auth_state"] == "unknown"
            assert by_name["unchecked"]["auth_actionable"] is False
            assert by_name["actionable"]["auth_state"] == "unhealthy"
            assert by_name["actionable"]["auth_actionable"] is True
            assert status["summary"]["unhealthy"] == 1
            assert status["summary"]["auth_disabled_or_unchecked_count"] == 4
    finally:
        async with async_session() as db:
            await db.execute(
                text(
                    "TRUNCATE user_subscription_sources, user_subscriptions, "
                    "subscription_sources, subscriptions, creators RESTART IDENTITY CASCADE"
                )
            )
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_membership_cache_does_not_replace_auth_attempt_health():
    from app.database import async_session, engine
    from app.models import Creator, Subscription, SubscriptionSource
    from app.services.subscription_membership import (
        recompute_subscription_membership_cache,
    )

    try:
        async with async_session() as db:
            await db.execute(
                text(
                    "TRUNCATE user_subscription_sources, user_subscriptions, "
                    "subscription_sources, subscriptions, creators RESTART IDENTITY CASCADE"
                )
            )
            creator = Creator(name="auth-cache")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="Auth cache")
            db.add(subscription)
            await db.flush()
            source = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id="auth-cache",
                source_url="https://www.pixiv.net/users/auth-cache",
                auth_healthy=True,
                auth_status="healthy",
                last_auth_checked_at=NOW,
            )
            db.add(source)
            await db.commit()

            await recompute_subscription_membership_cache(db, subscription.id)
            await db.refresh(source)

            assert source.auth_healthy is True
            assert source.auth_status == "healthy"
            assert source.last_auth_checked_at == NOW
    finally:
        async with async_session() as db:
            await db.execute(
                text(
                    "TRUNCATE user_subscription_sources, user_subscriptions, "
                    "subscription_sources, subscriptions, creators RESTART IDENTITY CASCADE"
                )
            )
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_auth_attention_counts_member_binding_failure_once():
    from app.database import async_session, engine
    from app.models import (
        Creator,
        RemoteAccount,
        Subscription,
        SubscriptionSource,
        User,
        UserSubscription,
        UserSubscriptionSource,
    )
    from app.services.auth_health import auth_attention_counts

    marker = f"auth-binding-{uuid4().hex}"
    try:
        async with async_session() as db:
            await db.execute(
                text(
                    "TRUNCATE user_subscription_sources, user_subscriptions, "
                    "subscription_sources, subscriptions, creators RESTART IDENTITY CASCADE"
                )
            )
            user = User(
                username=marker,
                password_hash="test-only",
                is_active=True,
            )
            creator = Creator(name=marker)
            db.add_all([user, creator])
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name=marker)
            db.add(subscription)
            await db.flush()
            source = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id="12345",
                source_url="https://www.pixiv.net/users/12345",
                is_enabled=True,
                auth_healthy=False,
                auth_status=None,
            )
            member = UserSubscription(
                user_id=user.id,
                subscription_id=subscription.id,
                name=marker,
                is_active=True,
                sync_enabled=True,
            )
            db.add_all([source, member])
            await db.flush()
            db.add(
                UserSubscriptionSource(
                    user_id=user.id,
                    subscription_id=subscription.id,
                    user_subscription_id=member.id,
                    subscription_source_id=source.id,
                    is_enabled=True,
                    auth_healthy=False,
                    auth_status="unhealthy",
                    auth_error_reason="HTTP 401 Unauthorized",
                    last_auth_checked_at=NOW,
                )
            )
            missing_account = RemoteAccount(
                user_id=user.id,
                source="pixiv",
                auth_method="refresh_token",
                credential_ciphertext=None,
                is_enabled=True,
                auth_status="healthy",
                last_authenticated_at=NOW,
            )
            missing_source = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id="12346",
                source_url="https://www.pixiv.net/users/12346",
                is_enabled=True,
                auth_healthy=True,
                auth_status=None,
            )
            db.add_all([missing_account, missing_source])
            await db.flush()
            db.add(
                UserSubscriptionSource(
                    user_id=user.id,
                    subscription_id=subscription.id,
                    user_subscription_id=member.id,
                    subscription_source_id=missing_source.id,
                    remote_account_id=missing_account.id,
                    is_enabled=True,
                    auth_healthy=True,
                    auth_status="healthy",
                    last_auth_checked_at=NOW,
                )
            )
            await db.commit()

            counts = await auth_attention_counts(db)

            assert counts == {
                "auth_actionable_count": 1,
                "auth_disabled_or_unchecked_count": 0,
                "credential_issue_count": 1,
            }

            from app.services.operation_attention import operations_overview
            from app.services.scheduler_decisions import decision_page

            overview = await operations_overview(db, view="attention")
            reasons = {
                (item.get("repository_id"), item.get("reason_code"))
                for item in overview["items"]
            }
            assert (str(source.id), "auth_unhealthy") in reasons
            assert (str(missing_source.id), "credential_missing") in reasons

            decisions = await decision_page(db, view="attention")
            decision_reasons = {
                (item["source_id"], item["reason"])
                for item in decisions["items"]
            }
            assert (str(source.id), "auth_unhealthy") in decision_reasons
            assert (
                str(missing_source.id),
                "credential_missing",
            ) in decision_reasons

            from app.api.admin.auth_health import get_auth_status

            auth_status = await get_auth_status(db)
            sources_by_id = {
                item["id"]: item for item in auth_status["sources"]
            }
            assert sources_by_id[str(source.id)]["auth_state"] == "unhealthy"
            assert sources_by_id[str(source.id)]["auth_actionable"] is True
            assert (
                sources_by_id[str(missing_source.id)]["credential_state"]
                == "missing"
            )
    finally:
        async with async_session() as db:
            await db.execute(
                text(
                    "TRUNCATE user_subscription_sources, user_subscriptions, "
                    "subscription_sources, subscriptions, creators RESTART IDENTITY CASCADE"
                )
            )
            await db.execute(
                text(
                    "DELETE FROM remote_accounts WHERE user_id IN "
                    "(SELECT id FROM users WHERE username = :marker)"
                ),
                {"marker": marker},
            )
            await db.execute(text("DELETE FROM users WHERE username = :marker"), {"marker": marker})
            await db.commit()
        await engine.dispose()
