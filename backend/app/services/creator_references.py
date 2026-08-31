"""Read-only aggregation of Pixiv identities and Danbooru aliases."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Creator, RemoteAccount, SourceCreator
from app.remote_discovery.common import RemoteReauthenticationRequired
from app.remote_discovery.contract import RemoteCreatorProfile
from app.remote_discovery.registry import DiscoveryAdapterRegistry, registry
from app.schemas.creator_reference import (
    CreatorReferencesRead,
    DanbooruCreatorReferenceRead,
    PixivCreatorReferenceRead,
)
from app.services import danbooru as danbooru_service
from app.services.remote_access_tokens import RemoteAccessTokenService
from app.services.remote_accounts import (
    RemoteAccountService,
    RemoteCredentialGenerationChanged,
    configured_credential_vault,
)
from app.services.remote_credentials import CredentialVault
from app.services.remote_discovery_rollout import preview_enabled


PIXIV_USER_RE = re.compile(r"(?:^|\.)pixiv\.net/(?:en/)?users/([0-9]+)(?:[/?#]|$)", re.I)


def _pixiv_id_from_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = PIXIV_USER_RE.search(value)
    return match.group(1) if match else None


def _stored_username(source: SourceCreator | None) -> str | None:
    metadata = source.raw_metadata if source and isinstance(source.raw_metadata, dict) else {}
    for key in ("username", "account", "screen_name"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lstrip("@")
    return None


def _stored_avatar(source: SourceCreator | None) -> str | None:
    metadata = source.raw_metadata if source and isinstance(source.raw_metadata, dict) else {}
    for key in ("avatar_url", "profile_image_url", "face"):
        value = metadata.get(key)
        if isinstance(value, str):
            return value
    images = metadata.get("profile_image_urls")
    if isinstance(images, Mapping):
        for key in ("medium", "square_medium", "large"):
            value = images.get(key)
            if isinstance(value, str):
                return value
    return None


class CreatorReferenceService:
    def __init__(
        self,
        db: AsyncSession,
        user_id: int,
        *,
        vault: CredentialVault | None = None,
        adapters: DiscoveryAdapterRegistry | None = None,
        tokens: RemoteAccessTokenService | None = None,
        danbooru_lookup: Callable[[int], dict | None] | None = None,
    ) -> None:
        self.db = db
        self.user_id = user_id
        self.vault = vault
        self.adapters = adapters or registry
        self.tokens = tokens or RemoteAccessTokenService()
        self.danbooru_lookup = danbooru_lookup or danbooru_service.get_artist

    async def _pixiv_account(self) -> RemoteAccount | None:
        if not preview_enabled("pixiv"):
            return None
        return (
            await self.db.execute(
                select(RemoteAccount)
                .where(
                    RemoteAccount.user_id == self.user_id,
                    RemoteAccount.source == "pixiv",
                    RemoteAccount.is_enabled.is_(True),
                    RemoteAccount.auth_status == "healthy",
                    RemoteAccount.credential_ciphertext.is_not(None),
                    or_(
                        RemoteAccount.auth_status.is_(None),
                        RemoteAccount.auth_status != "deleted",
                    ),
                )
                .order_by(RemoteAccount.id)
                .limit(1)
            )
        ).scalar_one_or_none()

    def _avatar_url(
        self,
        creator_id: UUID,
        upstream_url: str | None,
        account: RemoteAccount | None,
    ) -> str | None:
        if not upstream_url:
            return None
        try:
            token = self.tokens.issue_reference_media(
                user_id=self.user_id,
                creator_id=creator_id,
                upstream_url=upstream_url,
                remote_account_id=account.id if account else None,
                credential_generation=(
                    int(account.credential_generation) if account else None
                ),
            )
        except ValueError:
            return None
        return f"/api/v1/remote-media/{token}"

    async def get_references(self, creator_id: UUID) -> CreatorReferencesRead:
        creator = await self.db.get(Creator, creator_id)
        if creator is None:
            raise ValueError("Creator not found")
        local_sources = (
            await self.db.execute(
                select(SourceCreator)
                .where(
                    SourceCreator.creator_id == creator_id,
                    SourceCreator.source == "pixiv",
                )
                .order_by(SourceCreator.source_creator_id)
            )
        ).scalars().all()
        stored = {source.source_creator_id: source for source in local_sources}
        pixiv_ids = list(stored)

        artist: dict[str, Any] | None = None
        danbooru: DanbooruCreatorReferenceRead | None = None
        if creator.danbooru_artist_id is not None:
            try:
                artist = await asyncio.to_thread(
                    self.danbooru_lookup,
                    creator.danbooru_artist_id,
                )
            except Exception:
                artist = None
            if isinstance(artist, dict):
                for raw_url in artist.get("urls") or []:
                    if not isinstance(raw_url, Mapping):
                        continue
                    pixiv_id = _pixiv_id_from_url(
                        raw_url.get("normalized_url") or raw_url.get("url")
                    )
                    if pixiv_id and pixiv_id not in pixiv_ids:
                        pixiv_ids.append(pixiv_id)
                other_names = [
                    str(name)
                    for name in artist.get("other_names") or []
                    if isinstance(name, str) and name.strip()
                ]
                danbooru = DanbooruCreatorReferenceRead(
                    artist_id=creator.danbooru_artist_id,
                    name=str(artist.get("name") or "") or None,
                    other_names=list(dict.fromkeys(other_names)),
                    profile_url=(
                        f"https://danbooru.donmai.us/artists/{creator.danbooru_artist_id}"
                    ),
                    status="remote",
                )
            else:
                danbooru = DanbooruCreatorReferenceRead(
                    artist_id=creator.danbooru_artist_id,
                    profile_url=(
                        f"https://danbooru.donmai.us/artists/{creator.danbooru_artist_id}"
                    ),
                    status="fallback",
                )

        account = await self._pixiv_account()
        account_service: RemoteAccountService | None = None
        credentials = None
        pinned_identity = None
        pinned_generation = None
        if account is not None:
            try:
                account_service = RemoteAccountService(
                    self.db,
                    self.user_id,
                    vault=self.vault or configured_credential_vault(),
                    adapters=self.adapters,
                )
                pinned_identity = account_service._credential_use_identity(account)
                pinned_generation = int(account.credential_generation or 0)
                credentials = account_service.credentials_for_adapter(
                    account,
                    expected_generation=pinned_generation,
                )
            except Exception:
                account = None
                account_service = None
                credentials = None

        references: list[PixivCreatorReferenceRead] = []
        for pixiv_id in pixiv_ids:
            source = stored.get(pixiv_id)
            profile: RemoteCreatorProfile | None = None
            error_code = None
            if account is not None and account_service is not None and credentials is not None:
                try:
                    fetched = await self.adapters.get("pixiv").fetch_creator_profile(
                        credentials,
                        source_creator_id=pixiv_id,
                    )
                    if (
                        not isinstance(fetched, RemoteCreatorProfile)
                        or fetched.source != "pixiv"
                        or fetched.source_creator_id != pixiv_id
                    ):
                        raise ValueError("Pixiv profile identity mismatch")
                    account = await account_service._relock_provider_result(
                        account.id,
                        pinned_identity=pinned_identity,
                        pinned_generation=pinned_generation,
                    )
                    profile = fetched
                except RemoteCredentialGenerationChanged:
                    account = None
                    credentials = None
                    error_code = "credential_generation_changed"
                except RemoteReauthenticationRequired:
                    account = None
                    credentials = None
                    error_code = "reauthentication_required"
                except Exception:
                    error_code = "remote_profile_failed"
                    try:
                        account = await account_service._relock_provider_result(
                            account.id,
                            pinned_identity=pinned_identity,
                            pinned_generation=pinned_generation,
                        )
                    except RemoteCredentialGenerationChanged:
                        account = None
                        credentials = None
                        error_code = "credential_generation_changed"

            display_name = (
                profile.display_name
                if profile and profile.display_name
                else source.display_name
                if source and source.display_name
                else pixiv_id
            )
            username = profile.username if profile else _stored_username(source)
            upstream_avatar = profile.avatar_url if profile else _stored_avatar(source)
            references.append(
                PixivCreatorReferenceRead(
                    source_creator_id=pixiv_id,
                    display_name=display_name,
                    username=username,
                    profile_url=(
                        profile.profile_url
                        if profile
                        else f"https://www.pixiv.net/users/{pixiv_id}"
                    ),
                    avatar_url=self._avatar_url(
                        creator_id,
                        upstream_avatar,
                        account if profile else None,
                    ),
                    status="remote" if profile else "fallback",
                    error_code=error_code,
                )
            )
        return CreatorReferencesRead(pixiv=references, danbooru=danbooru)
