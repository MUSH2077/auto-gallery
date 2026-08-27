"""X remote-follow discovery via OAuth 2 official API or cookie fallback."""

from __future__ import annotations

from collections.abc import Mapping
from http.cookies import SimpleCookie
from typing import Any

from app.remote_discovery.common import (
    HttpxRemoteTransport,
    MalformedRemoteResponse,
    RemoteHTTPTransport,
    checked_payload,
    required_text,
    validate_page_size,
)
from app.remote_discovery.contract import DiscoveryPage, RemoteCandidateIdentity, RemoteCollection, RemoteDiscoveryAdapter
from app.services.remote_credentials import DownloadAuthenticationOverride


class XRemoteDiscoveryAdapter(RemoteDiscoveryAdapter):
    source = "x"
    auth_methods = ("oauth2", "cookie")
    required_oauth_scopes = (
        "users.read",
        "follows.read",
        "list.read",
        "offline.access",
    )
    API_BASE = "https://api.x.com/2"
    COOKIE_FOLLOWING_URL = "https://api.x.com/1.1/friends/list.json"

    def __init__(self, transport: RemoteHTTPTransport | None = None):
        self.transport = transport or HttpxRemoteTransport()

    def _auth_method(self, credentials: Mapping[str, Any]) -> str:
        method = str(credentials.get("auth_method") or "oauth2")
        if method not in self.auth_methods:
            raise ValueError("unsupported X discovery authentication method")
        return method

    async def validate_account(self, credentials: Mapping[str, Any]) -> RemoteCandidateIdentity:
        method = self._auth_method(credentials)
        if method == "oauth2":
            token = await self._oauth_access_token(credentials)
            response = await self.transport.request(
                "GET",
                f"{self.API_BASE}/users/me",
                headers={"Authorization": f"Bearer {token}"},
                params={"user.fields": "id,name,username,description,profile_image_url,url,verified,protected"},
            )
            payload = checked_payload(response, provider="X")
            user = payload.get("data")
        else:
            cookie = required_text(credentials, "cookie", provider="X")
            response = await self.transport.request(
                "GET",
                "https://api.x.com/1.1/account/verify_credentials.json",
                headers={"Cookie": cookie},
                params={"include_entities": "true", "skip_status": "true"},
            )
            user = checked_payload(response, provider="X")
        if not isinstance(user, Mapping):
            raise MalformedRemoteResponse("X account validation response has invalid data")
        return self._candidate(user, auth_method=method)

    async def _oauth_access_token(self, credentials: Mapping[str, Any]) -> str:
        token = credentials.get("access_token")
        if isinstance(token, str) and token:
            return token
        refresh_token = required_text(credentials, "refresh_token", provider="X")
        client_id = required_text(credentials, "client_id", provider="X")
        response = await self.transport.request(
            "POST",
            f"{self.API_BASE}/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
        )
        payload = checked_payload(response, provider="X")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise MalformedRemoteResponse("X token response is missing access_token")
        return token

    async def list_collections(self, credentials: Mapping[str, Any]) -> tuple[RemoteCollection, ...]:
        if self._auth_method(credentials) == "cookie":
            required_text(credentials, "cookie", provider="X")
            return (RemoteCollection("following", "Following", {"kind": "following"}),)
        user_id = required_text(credentials, "remote_user_id", provider="X")
        token = await self._oauth_access_token(credentials)
        response = await self.transport.request(
            "GET",
            f"{self.API_BASE}/users/{user_id}/owned_lists",
            headers={"Authorization": f"Bearer {token}"},
            params={"max_results": 100, "list.fields": "id,name,private,member_count"},
        )
        payload = checked_payload(response, provider="X")
        data = payload.get("data", [])
        if not isinstance(data, list):
            raise MalformedRemoteResponse("X owned lists response has invalid data")
        collections = [RemoteCollection("following", "Following", {"kind": "following"})]
        for item in data:
            if not isinstance(item, Mapping) or not item.get("id") or not item.get("name"):
                raise MalformedRemoteResponse("X owned lists response contains an invalid list")
            list_id = str(item["id"])
            collections.append(
                RemoteCollection(
                    f"list:{list_id}",
                    str(item["name"]),
                    {"kind": "list", "list_id": list_id, "private": bool(item.get("private"))},
                )
            )
        return tuple(collections)

    @staticmethod
    def _candidate(user: Mapping[str, Any], *, auth_method: str) -> RemoteCandidateIdentity:
        source_creator_id = str(user.get("id") or user.get("id_str") or "")
        username = str(user.get("username") or user.get("screen_name") or "")
        if not source_creator_id or not username:
            raise MalformedRemoteResponse("X following user is missing id or username")
        return RemoteCandidateIdentity(
            source="x",
            source_creator_id=source_creator_id,
            profile_url=f"https://x.com/{username}",
            display_name=str(user.get("name") or username),
            username=username,
            metadata={
                "auth_method": auth_method,
                "description": user.get("description"),
                "profile_image_url": user.get("profile_image_url") or user.get("profile_image_url_https"),
                "url": user.get("url"),
                "verified": user.get("verified"),
                "protected": user.get("protected"),
            },
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
        method = self._auth_method(credentials)
        selector = selector or {}
        collection_id = selector.get("collection_id")
        if not collection_id:
            if selector.get("kind") == "list" and selector.get("list_id"):
                collection_id = f"list:{selector['list_id']}"
            else:
                collection_id = "following"
        if method == "cookie":
            if collection_id != "following":
                raise ValueError("X cookie discovery supports only the following collection")
            return await self._discover_cookie(credentials, cursor=cursor, page_size=page_size)
        return await self._discover_oauth(
            credentials,
            collection_id=collection_id or "following",
            cursor=cursor,
            page_size=page_size,
        )

    async def _discover_oauth(self, credentials, *, collection_id, cursor, page_size):
        token = await self._oauth_access_token(credentials)
        params: dict[str, Any] = {
            "max_results": min(max(page_size, 1), 1000),
            "user.fields": "id,name,username,description,profile_image_url,url,verified,protected",
        }
        if cursor and cursor.get("pagination_token"):
            params["pagination_token"] = str(cursor["pagination_token"])
        if collection_id == "following":
            user_id = required_text(credentials, "remote_user_id", provider="X")
            url = f"{self.API_BASE}/users/{user_id}/following"
        elif collection_id.startswith("list:") and collection_id[5:]:
            url = f"{self.API_BASE}/lists/{collection_id[5:]}/members"
        else:
            raise ValueError("unknown X discovery collection")
        response = await self.transport.request(
            "GET", url, headers={"Authorization": f"Bearer {token}"}, params=params
        )
        payload = checked_payload(response, provider="X")
        data = payload.get("data", [])
        meta = payload.get("meta", {})
        if not isinstance(data, list) or not isinstance(meta, Mapping):
            raise MalformedRemoteResponse("X following response has invalid data or meta")
        items = [self._candidate(user, auth_method="oauth2") for user in data if isinstance(user, Mapping)]
        if len(items) != len(data):
            raise MalformedRemoteResponse("X following response contains an invalid user")
        next_token = meta.get("next_token")
        return DiscoveryPage(
            items=items,
            next_cursor={"pagination_token": str(next_token)} if next_token else None,
            done=not bool(next_token),
        )

    async def _discover_cookie(self, credentials, *, cursor, page_size):
        cookie = required_text(credentials, "cookie", provider="X")
        user_id = required_text(credentials, "remote_user_id", provider="X")
        response = await self.transport.request(
            "GET",
            self.COOKIE_FOLLOWING_URL,
            headers={"Cookie": cookie},
            params={
                "user_id": user_id,
                "cursor": str((cursor or {}).get("cursor", "-1")),
                "count": min(page_size, 200),
                "skip_status": "true",
                "include_user_entities": "true",
            },
        )
        payload = checked_payload(response, provider="X")
        users = payload.get("users")
        if not isinstance(users, list):
            raise MalformedRemoteResponse("X cookie following response has invalid users")
        items = [self._candidate(user, auth_method="cookie") for user in users if isinstance(user, Mapping)]
        if len(items) != len(users):
            raise MalformedRemoteResponse("X cookie following response contains an invalid user")
        next_cursor = str(payload.get("next_cursor_str") or payload.get("next_cursor") or "0")
        return DiscoveryPage(
            items=items,
            next_cursor={"cursor": next_cursor} if next_cursor not in {"0", "-1", ""} else None,
            done=next_cursor in {"0", "-1", ""},
        )

    def build_download_auth(self, credentials: Mapping[str, Any]) -> DownloadAuthenticationOverride:
        if self._auth_method(credentials) == "oauth2":
            token = required_text(credentials, "access_token", provider="X")
            return DownloadAuthenticationOverride({"headers": {"Authorization": f"Bearer {token}"}})
        raw_cookie = required_text(credentials, "cookie", provider="X")
        parsed = SimpleCookie()
        parsed.load(raw_cookie)
        cookies = {key: morsel.value for key, morsel in parsed.items()}
        if not cookies:
            raise ValueError("X cookie credential is malformed")
        return DownloadAuthenticationOverride({"extractor": {"twitter": {"cookies": cookies}}})
