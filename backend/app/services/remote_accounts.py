"""Account-private credential storage and remote adapter operations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import DiscoveryCandidate, RemoteAccount, UserSubscriptionSource
from app.remote_discovery.registry import DiscoveryAdapterRegistry, registry
from app.schemas.remote_discovery import RemoteAccountRead
from app.services.remote_credentials import CredentialVault, RedactedCredentials


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
        self.vault = vault or configured_credential_vault()
        self.adapters = adapters or registry

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

    def credentials_for_adapter(self, account: RemoteAccount) -> RedactedCredentials:
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
        return RedactedCredentials(values)

    async def create(self, data: dict[str, Any]) -> RemoteAccountRead:
        payload = dict(data)
        credentials = payload.pop("credentials", None)
        if not isinstance(credentials, dict) or not credentials:
            raise ValueError("Credentials are required")
        source = str(payload["source"])
        auth_method = str(payload.get("auth_method") or _DEFAULT_AUTH_METHOD[source])
        self._validate_auth_method(source, auth_method)
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
        account.scopes = payload.get("scopes") or []
        account.collection_selectors = payload.get("collection_selectors") or []
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
        credentials = data.get("credentials")
        requested_auth_method = data.get("auth_method") or account.auth_method or ""
        self._validate_auth_method(account.source, requested_auth_method)
        if requested_auth_method != account.auth_method and credentials is None:
            raise ValueError("Changing authentication method requires replacement credentials")
        account.auth_method = requested_auth_method
        for field in _ACCOUNT_UPDATE_FIELDS - {"auth_method"}:
            if field in data and data[field] is not None:
                setattr(account, field, data[field])
        if credentials is not None:
            self._encrypt(account, credentials)
            account.auth_status = "untested"
            account.auth_error_reason = None
        await self.db.flush()
        await self.db.refresh(account)
        return self._read(account)

    async def test(self, account_id: UUID) -> RemoteAccountRead:
        account = await self._account(account_id, lock=True)
        adapter = self.adapters.get(account.source)
        try:
            identity = await adapter.validate_account(self.credentials_for_adapter(account))
        except Exception as exc:
            account.auth_status = "unhealthy"
            account.auth_error_reason = type(exc).__name__
            await self.db.flush()
            raise
        if identity.source != account.source:
            raise ValueError("Remote adapter returned an identity for a different source")
        account.remote_user_id = identity.source_creator_id
        account.remote_username = identity.username or identity.display_name
        account.auth_status = "healthy"
        account.auth_error_reason = None
        account.last_authenticated_at = datetime.now(timezone.utc)
        await self.db.flush()
        await self.db.refresh(account)
        return self._read(account)

    async def collections(self, account_id: UUID):
        account = await self._account(account_id)
        return await self.adapters.get(account.source).list_collections(
            self.credentials_for_adapter(account)
        )

    async def delete(self, account_id: UUID) -> None:
        account = await self._account(account_id, lock=True)
        await self.db.execute(
            update(UserSubscriptionSource)
            .where(UserSubscriptionSource.remote_account_id == account.id)
            .values(remote_account_id=None)
        )
        imported_exists = (
            await self.db.execute(
                select(DiscoveryCandidate.id)
                .where(
                    DiscoveryCandidate.remote_account_id == account.id,
                    DiscoveryCandidate.state == "imported",
                )
                .limit(1)
            )
        ).scalar_one_or_none() is not None
        await self.db.execute(
            delete(DiscoveryCandidate).where(
                DiscoveryCandidate.remote_account_id == account.id,
                DiscoveryCandidate.state != "imported",
            )
        )
        account.credential_ciphertext = None
        account.credential_metadata = None
        account.credential_key_version = None
        if imported_exists:
            account.is_enabled = False
            account.auth_status = "deleted"
            account.auth_error_reason = None
            account.last_authenticated_at = None
            account.scan_cursor = None
            account.next_scan_at = None
            await self.db.flush()
            return
        await self.db.flush()
        await self.db.delete(account)
        await self.db.flush()
