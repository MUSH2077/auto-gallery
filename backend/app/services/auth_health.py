"""One authentication-health vocabulary for API and scheduler surfaces.

Authentication is evidence from a real provider attempt. Credential readiness
is configuration state. Keeping those axes separate prevents a missing member
binding (or a disabled source) from being presented as a failed login.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import and_, case, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.remote_discovery import (
    RemoteAccount,
    UserSubscription,
    UserSubscriptionSource,
)
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource


AuthState = Literal["healthy", "unhealthy", "unknown"]
CredentialState = Literal["ready", "missing", "not_required", "unknown"]

_HEALTHY_STATUSES = frozenset({"healthy", "ok", "success"})
_UNHEALTHY_STATUSES = frozenset(
    {
        "failed",
        "unhealthy",
        "reauth_required",
        "authentication_error",
        "credential_error",
        "deleted",
    }
)
_REMOTE_ACCOUNT_NOT_LOADED = object()


@dataclass(frozen=True)
class SourceHealth:
    auth_state: AuthState
    credential_state: CredentialState
    actionable: bool
    disabled_or_unchecked: bool
    credential_issue: bool


def _normalized_status(policy: Any) -> str:
    return str(getattr(policy, "auth_status", None) or "").strip().casefold()


def _has_failure_evidence(policy: Any) -> bool:
    return bool(
        getattr(policy, "last_auth_checked_at", None)
        or str(getattr(policy, "auth_error_reason", None) or "").strip()
    )


def auth_state_for(policy: Any) -> AuthState:
    """Classify only durable evidence produced by a real auth attempt."""

    status = _normalized_status(policy)
    if status in _HEALTHY_STATUSES:
        return "healthy"
    if status in _UNHEALTHY_STATUSES and _has_failure_evidence(policy):
        return "unhealthy"
    return "unknown"


def remote_account_auth_state_for(account: Any) -> AuthState:
    """Remote-account status is itself emitted only by an account check."""

    status = _normalized_status(account)
    if status in _HEALTHY_STATUSES:
        return "healthy"
    if status in _UNHEALTHY_STATUSES:
        return "unhealthy"
    return "unknown"


def credential_state_for(
    policy: Any,
    remote_account: Any = _REMOTE_ACCOUNT_NOT_LOADED,
) -> CredentialState:
    """Classify whether a source's selected private credential exists."""

    remote_account_id = getattr(policy, "remote_account_id", None)
    if remote_account_id is None:
        return "not_required"
    if remote_account is _REMOTE_ACCOUNT_NOT_LOADED:
        return "unknown"
    if remote_account is None:
        return "missing"
    if (
        getattr(remote_account, "id", None) != remote_account_id
        or not getattr(remote_account, "is_enabled", False)
        or not getattr(remote_account, "credential_ciphertext", None)
    ):
        return "missing"
    return "ready"


def classify_source_health(
    policy: Any,
    subscription_policy: Any,
    *,
    remote_account: Any = _REMOTE_ACCOUNT_NOT_LOADED,
) -> SourceHealth:
    auth_state = auth_state_for(policy)
    credential_state = credential_state_for(policy, remote_account)
    enabled_for_sync = bool(
        getattr(policy, "is_enabled", False)
        and getattr(subscription_policy, "is_active", False)
        and getattr(subscription_policy, "sync_enabled", False)
    )
    return SourceHealth(
        auth_state=auth_state,
        credential_state=credential_state,
        actionable=enabled_for_sync and auth_state == "unhealthy",
        disabled_or_unchecked=(not enabled_for_sync or auth_state == "unknown"),
        credential_issue=(enabled_for_sync and credential_state == "missing"),
    )


def source_health_payload(
    policy: Any,
    subscription_policy: Any,
    *,
    remote_account: Any = _REMOTE_ACCOUNT_NOT_LOADED,
) -> dict[str, str]:
    state = classify_source_health(
        policy,
        subscription_policy,
        remote_account=remote_account,
    )
    return {
        "auth_state": state.auth_state,
        "credential_state": state.credential_state,
    }


def auth_unhealthy_condition(model):
    """SQL equivalent of :func:`auth_state_for` for an ORM model."""

    failure_evidence = or_(
        model.last_auth_checked_at.is_not(None),
        func.nullif(func.btrim(model.auth_error_reason), "").is_not(None),
    )
    return and_(
        func.lower(func.coalesce(model.auth_status, "")).in_(
            _UNHEALTHY_STATUSES
        ),
        failure_evidence,
    )


def remote_account_unhealthy_condition(model):
    """SQL condition for an account-level authentication failure."""

    return func.lower(func.coalesce(model.auth_status, "")).in_(
        _UNHEALTHY_STATUSES
    )


def active_binding_exists(*conditions):
    """Correlated existence check for an enabled, syncing member binding."""

    return (
        select(1)
        .select_from(UserSubscriptionSource)
        .join(
            UserSubscription,
            UserSubscription.id == UserSubscriptionSource.user_subscription_id,
        )
        .where(
            UserSubscriptionSource.subscription_source_id
            == SubscriptionSource.id,
            UserSubscriptionSource.is_enabled.is_(True),
            UserSubscription.is_active.is_(True),
            UserSubscription.sync_enabled.is_(True),
            *conditions,
        )
        .correlate(SubscriptionSource)
        .exists()
    )


def actionable_binding_exists():
    return active_binding_exists(
        auth_unhealthy_condition(UserSubscriptionSource)
    )


def healthy_binding_exists():
    return active_binding_exists(
        auth_healthy_condition(UserSubscriptionSource)
    )


def credential_issue_binding_exists():
    return (
        select(1)
        .select_from(UserSubscriptionSource)
        .join(
            UserSubscription,
            UserSubscription.id == UserSubscriptionSource.user_subscription_id,
        )
        .outerjoin(
            RemoteAccount,
            RemoteAccount.id == UserSubscriptionSource.remote_account_id,
        )
        .where(
            UserSubscriptionSource.subscription_source_id
            == SubscriptionSource.id,
            UserSubscriptionSource.is_enabled.is_(True),
            UserSubscription.is_active.is_(True),
            UserSubscription.sync_enabled.is_(True),
            UserSubscriptionSource.remote_account_id.is_not(None),
            or_(
                RemoteAccount.id.is_(None),
                RemoteAccount.is_enabled.is_(False),
                RemoteAccount.credential_ciphertext.is_(None),
            ),
        )
        .correlate(SubscriptionSource)
        .exists()
    )


def auth_healthy_condition(model):
    """SQL condition for a recorded successful authentication attempt."""

    return func.lower(func.coalesce(model.auth_status, "")).in_(
        _HEALTHY_STATUSES
    )


def _auth_sql_conditions():
    unhealthy = auth_unhealthy_condition(SubscriptionSource)
    enabled_for_sync = and_(
        SubscriptionSource.is_enabled.is_(True),
        Subscription.is_active.is_(True),
        Subscription.sync_enabled.is_(True),
    )
    return enabled_for_sync, unhealthy


async def auth_attention_counts(db: AsyncSession) -> dict[str, int]:
    """Return all dashboard authentication counts in one database round trip."""

    enabled_for_sync, unhealthy = _auth_sql_conditions()
    active_binding = active_binding_exists()
    unhealthy_binding = actionable_binding_exists()
    healthy_binding = healthy_binding_exists()
    actionable = or_(
        and_(enabled_for_sync, unhealthy),
        unhealthy_binding,
    )
    active_policy = or_(enabled_for_sync, active_binding)
    recorded_healthy = or_(
        and_(enabled_for_sync, auth_healthy_condition(SubscriptionSource)),
        healthy_binding,
    )
    disabled_or_unchecked = and_(
        not_(actionable),
        or_(not_(active_policy), not_(recorded_healthy)),
    )
    credential_issues = (
        select(func.count(func.distinct(UserSubscriptionSource.subscription_source_id)))
        .select_from(UserSubscriptionSource)
        .join(
            UserSubscription,
            UserSubscription.id == UserSubscriptionSource.user_subscription_id,
        )
        .outerjoin(RemoteAccount, RemoteAccount.id == UserSubscriptionSource.remote_account_id)
        .where(
            UserSubscriptionSource.is_enabled.is_(True),
            UserSubscription.is_active.is_(True),
            UserSubscription.sync_enabled.is_(True),
            UserSubscriptionSource.remote_account_id.is_not(None),
            or_(
                RemoteAccount.id.is_(None),
                RemoteAccount.is_enabled.is_(False),
                RemoteAccount.credential_ciphertext.is_(None),
            ),
        )
        .scalar_subquery()
    )
    row = (
        await db.execute(
            select(
                func.coalesce(
                    func.sum(case((actionable, 1), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(case((disabled_or_unchecked, 1), else_=0)),
                    0,
                ),
                func.coalesce(credential_issues, 0),
            )
            .select_from(SubscriptionSource)
            .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
        )
    ).one()
    return {
        "auth_actionable_count": int(row[0] or 0),
        "auth_disabled_or_unchecked_count": int(row[1] or 0),
        "credential_issue_count": int(row[2] or 0),
    }
