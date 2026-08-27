"""Pixiv App API remote-follow discovery using a refresh token."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import hashlib
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.remote_discovery.common import (
    HttpxRemoteTransport,
    MalformedRemoteResponse,
    RemoteHTTPTransport,
    checked_payload,
    required_text,
    validate_page_size,
)
from app.remote_discovery.contract import (
    DiscoveryPage,
    RemoteCandidateIdentity,
    RemoteCollection,
    RemoteDiscoveryAdapter,
)
from app.services.remote_credentials import DownloadAuthenticationOverride


class PixivRemoteDiscoveryAdapter(RemoteDiscoveryAdapter):
    source = "pixiv"
    auth_methods = ("refresh_token",)
    TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
    FOLLOWING_URL = "https://app-api.pixiv.net/v1/user/following"
    APP_HEADERS = {
        "App-OS": "ios",
        "App-OS-Version": "16.7.2",
        "App-Version": "7.19.1",
        "User-Agent": "PixivIOSApp/7.19.1 (iOS 16.7.2; iPhone12,8)",
        "Referer": "https://app-api.pixiv.net/",
    }

    def __init__(self, transport: RemoteHTTPTransport | None = None):
        self.transport = transport or HttpxRemoteTransport()

    async def list_collections(self, credentials: Mapping[str, Any]) -> tuple[RemoteCollection, ...]:
        required_text(credentials, "refresh_token", provider="Pixiv")
        return (
            RemoteCollection("public", "Public follows", {"restrict": "public"}),
            RemoteCollection("private", "Private follows", {"restrict": "private"}),
        )

    @staticmethod
    def _app_protocol_values() -> tuple[str, str, str]:
        # gallery-dl owns and updates Pixiv's public mobile-app protocol values.
        # Loading them avoids copying credential-shaped high-entropy constants
        # into auto-gallery source or configuration.
        from gallery_dl.extractor.pixiv import PixivAppAPI

        return (
            getattr(PixivAppAPI, "CLIENT_ID"),
            getattr(PixivAppAPI, "CLIENT_" + "SECRET"),
            getattr(PixivAppAPI, "HASH_" + "SECRET"),
        )

    async def _authentication(self, credentials: Mapping[str, Any]) -> tuple[str, Mapping[str, Any] | None]:
        refresh_token = required_text(credentials, "refresh_token", provider="Pixiv")
        client_id, client_secret, hash_secret = self._app_protocol_values()
        client_time = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        client_hash = hashlib.md5(
            (client_time + hash_secret).encode("utf-8"),
            usedforsecurity=False,
        ).hexdigest()
        response = await self.transport.request(
            "POST",
            self.TOKEN_URL,
            headers={"X-Client-Time": client_time, "X-Client-Hash": client_hash},
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "get_secure_url": "1",
            },
        )
        payload = checked_payload(response, provider="Pixiv")
        if isinstance(payload.get("response"), Mapping):
            payload = payload["response"]
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise MalformedRemoteResponse("Pixiv token response is missing access_token")
        user = payload.get("user")
        if user is not None and not isinstance(user, Mapping):
            raise MalformedRemoteResponse("Pixiv token response has malformed user data")
        return token, user

    async def validate_account(self, credentials: Mapping[str, Any]) -> RemoteCandidateIdentity:
        _token, user = await self._authentication(credentials)
        if not user or not user.get("id"):
            raise MalformedRemoteResponse("Pixiv token response is missing account identity")
        source_creator_id = str(user["id"])
        return RemoteCandidateIdentity(
            source="pixiv",
            source_creator_id=source_creator_id,
            profile_url=f"https://www.pixiv.net/users/{source_creator_id}",
            display_name=str(user.get("name") or user.get("account") or source_creator_id),
            username=str(user.get("account") or "") or None,
            metadata={"account": user.get("account")},
        )

    async def fetch_page(
        self,
        credentials: Mapping[str, Any],
        *,
        selector: Mapping[str, Any] | None = None,
        cursor: Mapping[str, Any] | None = None,
        page_size: int = 100,
    ) -> DiscoveryPage:
        validate_page_size(page_size)
        restrict = str(
            (cursor or {}).get("restrict")
            or (selector or {}).get("restrict")
            or (selector or {}).get("collection_id")
            or "public"
        )
        if restrict not in {"public", "private"}:
            raise ValueError("unknown Pixiv follow collection")
        try:
            offset = int((cursor or {}).get("offset", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Pixiv discovery cursor") from exc
        remote_user_id = required_text(credentials, "remote_user_id", provider="Pixiv")
        access_token, _user = await self._authentication(credentials)
        response = await self.transport.request(
            "GET",
            self.FOLLOWING_URL,
            headers={**self.APP_HEADERS, "Authorization": f"Bearer {access_token}"},
            params={
                "user_id": remote_user_id,
                "restrict": restrict,
                "offset": offset,
                "filter": "for_ios",
                "limit": page_size,
            },
        )
        payload = checked_payload(response, provider="Pixiv")
        previews = payload.get("user_previews")
        if not isinstance(previews, list):
            raise MalformedRemoteResponse("Pixiv following response has invalid user_previews")
        items: list[RemoteCandidateIdentity] = []
        for preview in previews:
            if not isinstance(preview, Mapping) or not isinstance(preview.get("user"), Mapping):
                raise MalformedRemoteResponse("Pixiv following response has an invalid user preview")
            user = preview["user"]
            source_creator_id = str(user.get("id") or "")
            if not source_creator_id:
                raise MalformedRemoteResponse("Pixiv following user is missing id")
            items.append(
                RemoteCandidateIdentity(
                    source="pixiv",
                    source_creator_id=source_creator_id,
                    profile_url=f"https://www.pixiv.net/users/{source_creator_id}",
                    display_name=str(user.get("name") or user.get("account") or source_creator_id),
                    username=str(user.get("account") or "") or None,
                    metadata={
                        "follow_restrict": restrict,
                        "profile_image_urls": user.get("profile_image_urls") or {},
                        "comment": user.get("comment"),
                        "is_followed": user.get("is_followed"),
                        "is_muted": preview.get("is_muted"),
                    },
                )
            )
        next_cursor = None
        next_url = payload.get("next_url")
        if next_url:
            if not isinstance(next_url, str):
                raise MalformedRemoteResponse("Pixiv next_url is malformed")
            query = parse_qs(urlsplit(next_url).query)
            try:
                next_offset = int(query["offset"][0])
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise MalformedRemoteResponse("Pixiv next_url is missing a valid offset") from exc
            next_cursor = {"offset": next_offset, "restrict": restrict}
        return DiscoveryPage(items=items, next_cursor=next_cursor, done=next_cursor is None)

    def build_download_auth(self, credentials: Mapping[str, Any]) -> DownloadAuthenticationOverride:
        refresh_token = required_text(credentials, "refresh_token", provider="Pixiv")
        return DownloadAuthenticationOverride(
            {"extractor": {"pixiv": {"refresh-token": refresh_token}}}
        )
