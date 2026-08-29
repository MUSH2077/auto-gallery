"""Account-private credential storage and remote adapter operations."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import DiscoveryCandidate, RemoteAccount, UserSubscriptionSource
from app.remote_discovery.contract import RemoteWorkState
from app.remote_discovery.common import RemoteReauthenticationRequired
from app.remote_discovery.registry import DiscoveryAdapterRegistry, registry
from app.schemas.remote_discovery import RemoteAccountRead
from app.services.remote_credentials import (
    CredentialVault,
    RedactedCredentials,
    RefreshableCredentials,
)
from app.services.remote_discovery_rollout import require_auto_import, require_preview


_ACCOUNT_UPDATE_FIELDS = {
    "remote_user_id",
    "remote_username",
    "auth_method",
    "scopes",
    "collection_selectors",
    "is_enabled",
    "scan_interval_hours",
    "auto_import_enabled",
    "auto_import_min_confidence",
    "auto_import_limit",
}

_DEFAULT_AUTH_METHOD = {
    "pixiv": "refresh_token",
    "x": "oauth2",
    "bilibili": "sessdata",
}

_VALID_AUTH_METHODS = {
    "pixiv": frozenset({"refresh_token"}),
    "x": frozenset({"oauth2", "cookie"}),
    "bilibili": frozenset({"sessdata"}),
}

_X_SCOPES = frozenset({"users.read", "follows.read", "list.read", "offline.access"})
_MAX_SELECTORS = 200
_MAX_SELECTOR_BYTES = 64 * 1024
_NUMERIC_REMOTE_ID = re.compile(r"-?[0-9]{1,32}\Z")


class RemoteCredentialGenerationChanged(RuntimeError):
    """A credential consumer no longer owns the account generation it pinned."""


class RemoteWorkStateAccountRequired(RuntimeError):
    """No enabled credential-bearing remote account is available for this user."""


class RemoteWorkStateAccountUnhealthy(RuntimeError):
    """The user's remote account must be reauthenticated before use."""


def validate_remote_account_policy(
    source: str,
    scopes: Any,
    selectors: Any,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Validate the only non-secret provider policy shapes stored or echoed."""

    if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
        raise ValueError("Remote account scopes must be a list of strings")
    if len(scopes) != len(set(scopes)):
        raise ValueError("Remote account scopes must not contain duplicates")
    allowed_scopes = _X_SCOPES if source == "x" else frozenset()
    if set(scopes) - allowed_scopes:
        raise ValueError("Remote account scopes are not valid for this source")

    if not isinstance(selectors, list) or len(selectors) > _MAX_SELECTORS:
        raise ValueError("Remote account collection selectors exceed the allowed limit")
    try:
        encoded_size = len(
            json.dumps(selectors, ensure_ascii=False, separators=(",", ":")).encode()
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Remote account collection selectors must be JSON values") from exc
    if encoded_size > _MAX_SELECTOR_BYTES:
        raise ValueError("Remote account collection selectors are too large")

    validated: list[dict[str, Any]] = []
    for raw_selector in selectors:
        if not isinstance(raw_selector, dict):
            raise ValueError("Remote account collection selector must be an object")
        selector = dict(raw_selector)
        if source == "pixiv":
            if set(selector) != {"restrict"} or selector.get("restrict") not in {
                "public",
                "private",
            }:
                raise ValueError("Invalid Pixiv collection selector")
        elif source == "x":
            kind = selector.get("kind")
            if kind == "following":
                if set(selector) != {"kind"}:
                    raise ValueError("Invalid X following selector")
            elif kind == "list":
                if not {"kind", "list_id"} <= set(selector) or set(selector) - {
                    "kind",
                    "list_id",
                    "private",
                }:
                    raise ValueError("Invalid X list selector")
                list_id = selector.get("list_id")
                if not isinstance(list_id, str) or not _NUMERIC_REMOTE_ID.fullmatch(list_id):
                    raise ValueError("Invalid X list selector")
                if "private" in selector and not isinstance(selector["private"], bool):
                    raise ValueError("Invalid X list selector")
            else:
                raise ValueError("Invalid X collection selector")
        elif source == "bilibili":
            kind = selector.get("kind")
            if kind == "all":
                if set(selector) != {"kind"}:
                    raise ValueError("Invalid Bilibili all-following selector")
            elif kind == "group":
                if not {"kind", "group_id"} <= set(selector) or set(selector) - {
                    "kind",
                    "group_id",
                    "count",
                }:
                    raise ValueError("Invalid Bilibili group selector")
                group_id = selector.get("group_id")
                if not isinstance(group_id, str) or not _NUMERIC_REMOTE_ID.fullmatch(group_id):
                    raise ValueError("Invalid Bilibili group selector")
                count = selector.get("count")
                if "count" in selector and not (
                    count is None
                    or (
                        isinstance(count, int)
                        and not isinstance(count, bool)
                        and 0 <= count <= 2_147_483_647
                    )
                ):
                    raise ValueError("Invalid Bilibili group selector")
            else:
                raise ValueError("Invalid Bilibili collection selector")
        else:
            raise ValueError("Unsupported remote account source")
        validated.append(selector)
    return list(scopes), validated


def configured_credential_vault() -> CredentialVault:
    if not settings.remote_credential_key:
        raise RuntimeError("REMOTE_CREDENTIAL_KEY is required for remote accounts")
    return CredentialVault(settings.remote_credential_key)


class RemoteAccountService:
    def __init__(
        self,
        db: AsyncSession,
        user_id: int,
        *,
        vault: CredentialVault | None = None,
        adapters: DiscoveryAdapterRegistry | None = None,
    ):
        self.db = db
        self.user_id = user_id
        # Metadata reads and account deletion must remain available while a
        # rollout gate is closed, including recovery from a missing key. Only
        # credential-bearing operations resolve the configured vault.
        self._vault = vault
        self.adapters = adapters or registry

    @property
    def vault(self) -> CredentialVault:
        if self._vault is None:
            self._vault = configured_credential_vault()
        return self._vault

    async def _account(self, account_id: UUID, *, lock: bool = False) -> RemoteAccount:
        stmt = select(RemoteAccount).where(
            RemoteAccount.id == account_id,
            RemoteAccount.user_id == self.user_id,
            or_(RemoteAccount.auth_status.is_(None), RemoteAccount.auth_status != "deleted"),
        )
        if lock:
            stmt = stmt.with_for_update(of=RemoteAccount)
        account = (await self.db.execute(stmt)).scalar_one_or_none()
        if account is None:
            raise ValueError("Remote account not found")
        return account

    @staticmethod
    def _read(account: RemoteAccount) -> RemoteAccountRead:
        fields = []
        metadata = account.credential_metadata
        if isinstance(metadata, dict) and isinstance(metadata.get("fields"), list):
            fields = [str(field) for field in metadata["fields"]]
        return RemoteAccountRead.model_validate(
            {
                "id": account.id,
                "user_id": account.user_id,
                "source": account.source,
                "remote_user_id": account.remote_user_id,
                "remote_username": account.remote_username,
                "auth_method": account.auth_method,
                "scopes": account.scopes or [],
                "collection_selectors": account.collection_selectors or [],
                "is_enabled": account.is_enabled,
                "auth_status": account.auth_status,
                "auth_error_reason": account.auth_error_reason,
                "last_authenticated_at": account.last_authenticated_at,
                "last_scan_started_at": account.last_scan_started_at,
                "last_scan_completed_at": account.last_scan_completed_at,
                "next_scan_at": account.next_scan_at,
                "scan_interval_hours": account.scan_interval_hours,
                "auto_import_enabled": account.auto_import_enabled,
                "auto_import_min_confidence": account.auto_import_min_confidence,
                "auto_import_limit": account.auto_import_limit,
                "has_credentials": bool(account.credential_ciphertext),
                "credential_mask": {field: "••••" for field in fields},
                "created_at": account.created_at,
                "updated_at": account.updated_at,
            }
        )

    @staticmethod
    def _validate_auth_method(source: str, auth_method: str) -> None:
        if auth_method not in _VALID_AUTH_METHODS.get(source, frozenset()):
            raise ValueError("Authentication method is not valid for this source")

    @staticmethod
    def _validate_credentials(source: str, auth_method: str, credentials: dict[str, str]) -> None:
        RemoteAccountService._validate_auth_method(source, auth_method)
        allowed_fields = {
            ("pixiv", "refresh_token"): {"refresh_token"},
            ("x", "oauth2"): {"access_token", "refresh_token", "client_id"},
            ("x", "cookie"): {"cookie"},
            ("bilibili", "sessdata"): {"SESSDATA"},
        }[(source, auth_method)]
        if set(credentials) - allowed_fields:
            raise ValueError("Credentials include unexpected credential fields")
        required = {
            ("pixiv", "refresh_token"): ("refresh_token",),
            ("x", "oauth2"): (),
            ("x", "cookie"): ("cookie",),
            ("bilibili", "sessdata"): ("SESSDATA",),
        }.get((source, auth_method))
        if required is None:  # pragma: no cover - guarded by _validate_auth_method
            raise ValueError("Authentication method is not valid for this source")
        if auth_method == "oauth2" and not (
            credentials.get("access_token") or credentials.get("refresh_token")
        ):
            raise ValueError("X OAuth credentials require an access or refresh token")
        for field in required:
            if not isinstance(credentials.get(field), str) or not credentials[field].strip():
                raise ValueError(f"Credential field {field!r} is required")
        if any(not isinstance(value, str) or not value for value in credentials.values()):
            raise ValueError("Credential values must be non-empty strings")

    def _encrypt(self, account: RemoteAccount, credentials: dict[str, str]) -> None:
        self._validate_credentials(account.source, account.auth_method or "", credentials)
        account.credential_ciphertext = self.vault.encrypt(
            credentials,
            user_id=account.user_id,
            source=account.source,
            account_id=account.id,
        )
        account.credential_key_version = 1
        account.credential_metadata = {"fields": sorted(credentials)}
        account.credential_generation = int(account.credential_generation or 0) + 1

    def credentials_for_adapter(
        self,
        account: RemoteAccount,
        *,
        expected_generation: int | None = None,
        on_generation_advanced: Callable[[int], Awaitable[None]] | None = None,
    ) -> RedactedCredentials:
        if not account.credential_ciphertext:
            raise ValueError("Remote account has no credentials")
        values = self.vault.decrypt(
            account.credential_ciphertext,
            user_id=account.user_id,
            source=account.source,
            account_id=account.id,
        ).materialize()
        values["auth_method"] = account.auth_method
        if account.remote_user_id:
            values["remote_user_id"] = account.remote_user_id
        if account.source != "x" or account.auth_method != "oauth2":
            return RedactedCredentials(values)

        pinned_identity = (
            account.id,
            account.user_id,
            account.source,
            account.auth_method,
            account.remote_user_id,
        )
        state = {
            "generation": int(
                account.credential_generation
                if expected_generation is None
                else expected_generation
            )
        }

        async def rotate(
            request_rotation: Callable[
                [Mapping[str, Any]], Awaitable[Mapping[str, Any]]
            ],
        ) -> Mapping[str, Any]:
            current = (
                await self.db.execute(
                    select(RemoteAccount)
                    .where(RemoteAccount.id == account.id)
                    .with_for_update(of=RemoteAccount)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if current is None or (
                current.id,
                current.user_id,
                current.source,
                current.auth_method,
                current.remote_user_id,
            ) != pinned_identity:
                raise RemoteCredentialGenerationChanged(
                    "remote account identity changed during credential use"
                )

            current_generation = int(current.credential_generation or 0)
            if current_generation != state["generation"]:
                rotation = (
                    (current.credential_metadata or {}).get("oauth_refresh")
                    if isinstance(current.credential_metadata, dict)
                    else None
                )
                internally_rotated = bool(
                    isinstance(rotation, dict)
                    and rotation.get("previous_generation") == state["generation"]
                    and current_generation == state["generation"] + 1
                )
                if not internally_rotated:
                    raise RemoteCredentialGenerationChanged(
                        "remote account credentials changed during credential use"
                    )
                state["generation"] = current_generation
                if on_generation_advanced is not None:
                    await on_generation_advanced(current_generation)
                latest = self.vault.decrypt(
                    current.credential_ciphertext,
                    user_id=current.user_id,
                    source=current.source,
                    account_id=current.id,
                ).materialize()
                latest["auth_method"] = current.auth_method
                if current.remote_user_id:
                    latest["remote_user_id"] = current.remote_user_id
                return latest

            stored = self.vault.decrypt(
                current.credential_ciphertext,
                user_id=current.user_id,
                source=current.source,
                account_id=current.id,
            ).materialize()
            ephemeral = {
                **stored,
                "auth_method": current.auth_method,
            }
            if current.remote_user_id:
                ephemeral["remote_user_id"] = current.remote_user_id
            rotated = dict(await request_rotation(ephemeral))
            persisted = {
                key: rotated[key]
                for key in ("access_token", "refresh_token", "client_id")
                if isinstance(rotated.get(key), str) and rotated[key]
            }
            self._validate_credentials("x", "oauth2", persisted)
            previous_generation = state["generation"]
            current.credential_ciphertext = self.vault.encrypt(
                persisted,
                user_id=current.user_id,
                source=current.source,
                account_id=current.id,
            )
            current.credential_key_version = 1
            current.credential_metadata = {
                "fields": sorted(persisted),
                "oauth_refresh": {
                    "previous_generation": previous_generation,
                },
            }
            current.credential_generation = previous_generation + 1
            state["generation"] = current.credential_generation
            if on_generation_advanced is not None:
                await on_generation_advanced(current.credential_generation)
            await self.db.flush()
            # Providers may invalidate the old refresh token immediately. Make
            # the encrypted replacement durable before the single API retry.
            await self.db.commit()
            result = dict(persisted)
            result["auth_method"] = current.auth_method
            if current.remote_user_id:
                result["remote_user_id"] = current.remote_user_id
            return result

        return RefreshableCredentials(values, rotate)

    @staticmethod
    def _credential_use_identity(account: RemoteAccount) -> tuple[Any, ...]:
        return (
            account.id,
            account.user_id,
            account.source,
            account.auth_method,
            account.remote_user_id,
        )

    async def _relock_provider_result(
        self,
        account_id: UUID,
        *,
        pinned_identity: tuple[Any, ...],
        pinned_generation: int,
    ) -> RemoteAccount:
        """Reject a provider result superseded while an OAuth retry was in flight."""

        current = (
            await self.db.execute(
                select(RemoteAccount)
                .where(RemoteAccount.id == account_id)
                .with_for_update(of=RemoteAccount)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            current is None
            or current.auth_status == "deleted"
            or self._credential_use_identity(current) != pinned_identity
            or int(current.credential_generation or 0) != pinned_generation
        ):
            raise RemoteCredentialGenerationChanged(
                "remote account changed while provider request was running"
            )
        return current

    async def _set_binding_health(
        self,
        account: RemoteAccount,
        *,
        healthy: bool,
        checked_at: datetime | None = None,
        failure_reason: str = "Account validation failed",
    ) -> None:
        """Update every binding for one owned account and refresh shared caches."""

        checked_at = checked_at or datetime.now(timezone.utc)
        bindings = await self._locked_account_bindings(account.id)
        for binding in bindings:
            binding.auth_healthy = healthy
            binding.auth_status = "healthy" if healthy else "unhealthy"
            binding.auth_error_reason = None if healthy else failure_reason
            binding.last_auth_checked_at = checked_at
        await self._recompute_binding_caches(bindings)

    async def _locked_account_bindings(
        self,
        account_id: UUID,
    ) -> list[UserSubscriptionSource]:
        """Lock bindings only after the caller has locked this account row.

        Credential lifecycle and download outcomes share the deterministic
        ``RemoteAccount -> UserSubscriptionSource -> canonical aggregate``
        order documented in ``subscription_enqueue.mark_source_sync_success``.
        """

        bindings = list(
            (
                await self.db.execute(
                    select(UserSubscriptionSource)
                    .where(UserSubscriptionSource.remote_account_id == account_id)
                    .order_by(UserSubscriptionSource.id)
                    .with_for_update(of=UserSubscriptionSource)
                )
            ).scalars()
        )
        return bindings

    async def _recompute_binding_caches(
        self,
        bindings: list[UserSubscriptionSource],
    ) -> None:
        from app.services.subscription_membership import (
            recompute_subscription_membership_cache,
        )

        for subscription_id in sorted(
            {binding.subscription_id for binding in bindings},
            key=str,
        ):
            await recompute_subscription_membership_cache(self.db, subscription_id)

    async def create(self, data: dict[str, Any]) -> RemoteAccountRead:
        payload = dict(data)
        credentials = payload.pop("credentials", None)
        if not isinstance(credentials, dict) or not credentials:
            raise ValueError("Credentials are required")
        source = str(payload["source"])
        require_preview(source)
        if payload.get("auto_import_enabled"):
            require_auto_import(source)
        auth_method = str(payload.get("auth_method") or _DEFAULT_AUTH_METHOD[source])
        self._validate_auth_method(source, auth_method)
        self._validate_credentials(source, auth_method, credentials)
        scopes, selectors = validate_remote_account_policy(
            source,
            payload.get("scopes") or [],
            payload.get("collection_selectors") or [],
        )
        existing = (
            await self.db.execute(
                select(RemoteAccount)
                .where(
                    RemoteAccount.user_id == self.user_id,
                    RemoteAccount.source == source,
                )
                .with_for_update(of=RemoteAccount)
            )
        ).scalar_one_or_none()
        if existing is not None and existing.auth_status != "deleted":
            raise ValueError("A remote account for this source already exists")
        account = existing or RemoteAccount(user_id=self.user_id, source=source)
        account.auth_method = auth_method
        account.remote_user_id = payload.get("remote_user_id")
        account.remote_username = payload.get("remote_username")
        account.scopes = scopes
        account.collection_selectors = selectors
        account.is_enabled = payload.get("is_enabled", True)
        account.scan_interval_hours = payload.get("scan_interval_hours", 24)
        account.auto_import_enabled = payload.get("auto_import_enabled", False)
        account.auto_import_min_confidence = payload.get("auto_import_min_confidence", "high")
        account.auto_import_limit = payload.get("auto_import_limit", 25)
        account.auth_status = "untested"
        account.auth_error_reason = None
        account.last_authenticated_at = None
        account.scan_cursor = None
        account.last_scan_started_at = None
        account.last_scan_completed_at = None
        account.next_scan_at = None
        if existing is None:
            self.db.add(account)
        await self.db.flush()
        self._encrypt(account, credentials)
        if existing is not None:
            # A tombstoned account keeps imported provenance/binding ownership.
            # Reconnection remains ineligible until explicit validation heals
            # those bindings, but canonical caches must see the revived account.
            await self._recompute_binding_caches(
                await self._locked_account_bindings(account.id)
            )
        await self.db.flush()
        await self.db.refresh(account)
        return self._read(account)

    async def list(self, *, offset: int = 0, limit: int = 50) -> list[RemoteAccountRead]:
        accounts = (
            await self.db.execute(
                select(RemoteAccount)
                .where(
                    RemoteAccount.user_id == self.user_id,
                    or_(
                        RemoteAccount.auth_status.is_(None),
                        RemoteAccount.auth_status != "deleted",
                    ),
                )
                .order_by(RemoteAccount.source, RemoteAccount.id)
                .offset(max(0, offset))
                .limit(max(1, min(limit, 200)))
            )
        ).scalars().all()
        return [self._read(account) for account in accounts]

    async def get(self, account_id: UUID) -> RemoteAccountRead:
        return self._read(await self._account(account_id))

    async def update(self, account_id: UUID, data: dict[str, Any]) -> RemoteAccountRead:
        account = await self._account(account_id, lock=True)
        require_preview(account.source)
        if data.get("auto_import_enabled"):
            require_auto_import(account.source)
        was_enabled = account.is_enabled
        credentials = data.get("credentials")
        requested_auth_method = data.get("auth_method") or account.auth_method or ""
        self._validate_auth_method(account.source, requested_auth_method)
        if requested_auth_method != account.auth_method and credentials is None:
            raise ValueError("Changing authentication method requires replacement credentials")
        if credentials is not None:
            self._validate_credentials(account.source, requested_auth_method, credentials)
        scopes, selectors = validate_remote_account_policy(
            account.source,
            data.get("scopes") if data.get("scopes") is not None else account.scopes or [],
            data.get("collection_selectors")
            if data.get("collection_selectors") is not None
            else account.collection_selectors or [],
        )
        account.auth_method = requested_auth_method
        for field in _ACCOUNT_UPDATE_FIELDS - {
            "auth_method",
            "scopes",
            "collection_selectors",
        }:
            if field in data and data[field] is not None:
                setattr(account, field, data[field])
        account.scopes = scopes
        account.collection_selectors = selectors
        if credentials is not None:
            self._encrypt(account, credentials)
            account.auth_status = "untested"
            account.auth_error_reason = None
            await self._set_binding_health(account, healthy=True)
        elif account.is_enabled != was_enabled:
            await self._recompute_binding_caches(
                await self._locked_account_bindings(account.id)
            )
        await self.db.flush()
        await self.db.refresh(account)
        return self._read(account)

    async def test(self, account_id: UUID) -> RemoteAccountRead:
        account = await self._account(account_id, lock=True)
        require_preview(account.source)
        adapter = self.adapters.get(account.source)
        pinned_identity = self._credential_use_identity(account)
        pinned_generation = int(account.credential_generation or 0)

        async def advance_generation(generation: int) -> None:
            nonlocal pinned_generation
            pinned_generation = generation

        try:
            credentials = self.credentials_for_adapter(
                account,
                expected_generation=pinned_generation,
                on_generation_advanced=advance_generation,
            )
            identity = await adapter.validate_account(credentials)
            if identity.source != account.source:
                raise ValueError("Remote adapter returned an identity for a different source")
        except RemoteCredentialGenerationChanged:
            raise
        except Exception as exc:
            account = await self._relock_provider_result(
                account_id,
                pinned_identity=pinned_identity,
                pinned_generation=pinned_generation,
            )
            reauthentication = isinstance(exc, RemoteReauthenticationRequired)
            account.auth_status = "unhealthy"
            account.auth_error_reason = (
                "reauthentication_required"
                if reauthentication
                else "Account validation failed"
            )
            await self._set_binding_health(
                account,
                healthy=False,
                failure_reason=account.auth_error_reason,
            )
            await self.db.flush()
            raise
        account = await self._relock_provider_result(
            account_id,
            pinned_identity=pinned_identity,
            pinned_generation=pinned_generation,
        )
        checked_at = datetime.now(timezone.utc)
        account.remote_user_id = identity.source_creator_id
        account.remote_username = identity.username or identity.display_name
        account.auth_status = "healthy"
        account.auth_error_reason = None
        account.last_authenticated_at = checked_at
        await self._set_binding_health(account, healthy=True, checked_at=checked_at)
        await self.db.flush()
        await self.db.refresh(account)
        return self._read(account)

    async def collections(self, account_id: UUID):
        account = await self._account(account_id, lock=True)
        require_preview(account.source)
        pinned_identity = self._credential_use_identity(account)
        pinned_generation = int(account.credential_generation or 0)

        async def advance_generation(generation: int) -> None:
            nonlocal pinned_generation
            pinned_generation = generation

        try:
            collections = await self.adapters.get(account.source).list_collections(
                self.credentials_for_adapter(
                    account,
                    expected_generation=pinned_generation,
                    on_generation_advanced=advance_generation,
                )
            )
        except RemoteCredentialGenerationChanged:
            raise
        except Exception as exc:
            account = await self._relock_provider_result(
                account_id,
                pinned_identity=pinned_identity,
                pinned_generation=pinned_generation,
            )
            if not isinstance(exc, RemoteReauthenticationRequired):
                raise
            account.auth_status = "unhealthy"
            account.auth_error_reason = "reauthentication_required"
            await self._set_binding_health(
                account,
                healthy=False,
                failure_reason="reauthentication_required",
            )
            await self.db.flush()
            # The collections API has no success-path write. Persist the
            # account-local failure before returning its generic 502.
            await self.db.commit()
            raise
        await self._relock_provider_result(
            account_id,
            pinned_identity=pinned_identity,
            pinned_generation=pinned_generation,
        )
        return collections

    async def fetch_work_state(self, source: str, source_work_id: str) -> RemoteWorkState:
        """Fetch volatile work state using only this user's healthy account."""

        require_preview(source)
        account = (
            await self.db.execute(
                select(RemoteAccount)
                .where(
                    RemoteAccount.user_id == self.user_id,
                    RemoteAccount.source == source,
                    RemoteAccount.auth_status != "deleted",
                )
                .order_by(RemoteAccount.id)
            )
        ).scalar_one_or_none()
        if account is None or not account.is_enabled or not account.credential_ciphertext:
            raise RemoteWorkStateAccountRequired
        if account.auth_status != "healthy":
            raise RemoteWorkStateAccountUnhealthy

        account_id = account.id
        pinned_identity = self._credential_use_identity(account)
        pinned_generation = int(account.credential_generation or 0)

        async def advance_generation(generation: int) -> None:
            nonlocal pinned_generation
            pinned_generation = generation

        try:
            state = await self.adapters.get(source).fetch_work_state(
                self.credentials_for_adapter(
                    account,
                    expected_generation=pinned_generation,
                    on_generation_advanced=advance_generation,
                ),
                source_work_id=source_work_id,
            )
            if state.source != source or state.source_work_id != source_work_id:
                raise ValueError("Remote adapter returned work state for a different work")
        except RemoteCredentialGenerationChanged:
            raise
        except Exception as exc:
            account = await self._relock_provider_result(
                account_id,
                pinned_identity=pinned_identity,
                pinned_generation=pinned_generation,
            )
            if (
                not account.is_enabled
                or not account.credential_ciphertext
                or account.auth_status != "healthy"
            ):
                raise RemoteCredentialGenerationChanged(
                    "remote account eligibility changed while provider request was running"
                )
            if not isinstance(exc, RemoteReauthenticationRequired):
                raise
            account.auth_status = "unhealthy"
            account.auth_error_reason = "reauthentication_required"
            await self._set_binding_health(
                account,
                healthy=False,
                failure_reason="reauthentication_required",
            )
            await self.db.flush()
            raise

        account = await self._relock_provider_result(
            account_id,
            pinned_identity=pinned_identity,
            pinned_generation=pinned_generation,
        )
        if (
            not account.is_enabled
            or not account.credential_ciphertext
            or account.auth_status != "healthy"
        ):
            raise RemoteCredentialGenerationChanged(
                "remote account eligibility changed while provider request was running"
            )
        return state

    async def delete(self, account_id: UUID) -> None:
        account = await self._account(account_id, lock=True)
        # Lifecycle/delete/import order for one account is:
        # RemoteAccount -> DiscoveryCandidate (id order) ->
        # UserSubscriptionSource (id order) -> canonical aggregate.  Import
        # starts at its candidate and never locks the account, so holding no
        # member row while waiting for the candidate removes the old
        # Candidate <-> USS cycle.
        candidates = list(
            (
                await self.db.execute(
                    select(DiscoveryCandidate)
                    .where(DiscoveryCandidate.remote_account_id == account.id)
                    .order_by(DiscoveryCandidate.id)
                    .with_for_update(of=DiscoveryCandidate)
                )
            ).scalars()
        )
        bindings = await self._locked_account_bindings(account.id)
        imported_exists = any(candidate.state == "imported" for candidate in candidates)
        await self.db.execute(
            delete(DiscoveryCandidate).where(
                DiscoveryCandidate.remote_account_id == account.id,
                DiscoveryCandidate.state != "imported",
            )
        )
        account.credential_generation = int(account.credential_generation or 0) + 1
        account.credential_ciphertext = None
        account.credential_metadata = None
        account.credential_key_version = None
        checked_at = datetime.now(timezone.utc)
        for binding in bindings:
            binding.auth_healthy = False
            binding.auth_status = "deleted"
            binding.auth_error_reason = "Remote account deleted"
            binding.last_auth_checked_at = checked_at
            binding.next_sync_at = None
        if imported_exists:
            account.is_enabled = False
            account.auth_status = "deleted"
            account.auth_error_reason = None
            account.last_authenticated_at = None
            account.scan_cursor = None
            account.next_scan_at = None
            await self.db.flush()
            await self._recompute_binding_caches(bindings)
            return
        # Hard deletion needs the nullable FK cleared before PostgreSQL can
        # remove the account.  The unhealthy/deleted binding state is retained
        # so this can never masquerade as a migrated legacy NULL credential.
        for binding in bindings:
            binding.remote_account_id = None
        await self.db.flush()
        await self.db.delete(account)
        await self.db.flush()
        await self._recompute_binding_caches(bindings)
