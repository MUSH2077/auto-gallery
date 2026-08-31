"""Bilibili following/group discovery authenticated by SESSDATA."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.remote_discovery.common import (
    HttpxRemoteTransport,
    MalformedRemoteResponse,
    RemoteHTTPTransport,
    RemoteRateLimited,
    RemoteReauthenticationRequired,
    checked_payload,
    required_text,
    validate_page_size,
)
from app.remote_discovery.contract import DiscoveryPage, RemoteCandidateIdentity, RemoteCollection, RemoteDiscoveryAdapter
from app.services.remote_credentials import DownloadAuthenticationOverride


class BilibiliRemoteDiscoveryAdapter(RemoteDiscoveryAdapter):
    source = "bilibili"
    auth_methods = ("sessdata",)
    API_BASE = "https://api.bilibili.com"

    def __init__(self, transport: RemoteHTTPTransport | None = None):
        self.transport = transport or HttpxRemoteTransport()

    def _headers(self, credentials: Mapping[str, Any]) -> dict[str, str]:
        sessdata = required_text(credentials, "SESSDATA", provider="Bilibili")
        return {"Cookie": f"SESSDATA={sessdata}"}

    async def validate_account(self, credentials: Mapping[str, Any]) -> RemoteCandidateIdentity:
        response = await self.transport.request(
            "GET",
            f"{self.API_BASE}/x/web-interface/nav",
            headers=self._headers(credentials),
        )
        payload = self._api_payload(response, operation="account validation")
        user = payload.get("data")
        if not isinstance(user, Mapping):
            raise MalformedRemoteResponse("Bilibili account validation response has invalid data")
        if user.get("isLogin") is False or not user.get("mid"):
            raise RemoteReauthenticationRequired(
                401,
                "Bilibili SESSDATA requires reauthentication",
            )
        source_creator_id = str(user["mid"])
        return RemoteCandidateIdentity(
            source="bilibili",
            source_creator_id=source_creator_id,
            profile_url=f"https://space.bilibili.com/{source_creator_id}/dynamic",
            display_name=str(user.get("uname") or source_creator_id),
            username=None,
            metadata={"face": user.get("face"), "sign": user.get("sign")},
        )

    @staticmethod
    def _api_payload(response, *, operation: str) -> Mapping[str, Any]:
        payload = checked_payload(response, provider="Bilibili")
        code = payload.get("code")
        if code in {-101, -111}:
            raise RemoteReauthenticationRequired(401, "Bilibili SESSDATA requires reauthentication")
        if code == -412:
            raise RemoteRateLimited(None)
        if code != 0:
            raise MalformedRemoteResponse(f"Bilibili {operation} failed with API code {code!r}")
        return payload

    async def list_collections(self, credentials: Mapping[str, Any]) -> tuple[RemoteCollection, ...]:
        user_id = required_text(credentials, "remote_user_id", provider="Bilibili")
        response = await self.transport.request(
            "GET",
            f"{self.API_BASE}/x/relation/tags",
            headers=self._headers(credentials),
            params={"mid": user_id},
        )
        payload = self._api_payload(response, operation="following groups")
        data = payload.get("data")
        if not isinstance(data, list):
            raise MalformedRemoteResponse("Bilibili following groups response has invalid data")
        collections = [RemoteCollection("all", "All following", {"kind": "all"})]
        for group in data:
            if not isinstance(group, Mapping) or group.get("tagid") is None or not group.get("name"):
                raise MalformedRemoteResponse("Bilibili following groups contains an invalid group")
            group_id = str(group["tagid"])
            collections.append(
                RemoteCollection(
                    f"group:{group_id}",
                    str(group["name"]),
                    {"kind": "group", "group_id": group_id, "count": group.get("count")},
                )
            )
        return tuple(collections)

    async def fetch_page(
        self,
        credentials: Mapping[str, Any],
        *,
        selector: Mapping[str, Any] | None = None,
        cursor: Mapping[str, Any] | None = None,
        page_size: int = 100,
    ) -> DiscoveryPage:
        validate_page_size(page_size)
        try:
            page = int((cursor or {}).get("page", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Bilibili discovery cursor") from exc
        if page < 1:
            raise ValueError("invalid Bilibili discovery cursor")
        user_id = required_text(credentials, "remote_user_id", provider="Bilibili")
        params: dict[str, Any] = {"mid": user_id, "pn": page, "ps": min(page_size, 50)}
        selector = selector or {}
        collection_id = selector.get("collection_id")
        kind = selector.get("kind")
        group_id: str | None = None
        if collection_id in {None, "all"} and kind in {None, "all"}:
            url = f"{self.API_BASE}/x/relation/followings"
            params["vmid"] = params.pop("mid")
            params["order_type"] = ""
        elif kind == "group" and selector.get("group_id"):
            group_id = str(selector["group_id"])
            url = f"{self.API_BASE}/x/relation/tag"
            params["tagid"] = group_id
        elif isinstance(collection_id, str) and collection_id.startswith("group:") and collection_id[6:]:
            group_id = collection_id[6:]
            url = f"{self.API_BASE}/x/relation/tag"
            params["tagid"] = group_id
        else:
            raise ValueError("unknown Bilibili following collection")
        response = await self.transport.request(
            "GET", url, headers=self._headers(credentials), params=params
        )
        payload = self._api_payload(response, operation="following")
        data = payload.get("data")
        if isinstance(data, Mapping) and isinstance(data.get("list"), list):
            users = data["list"]
            raw_total = data.get("total", len(users))
        elif group_id is not None and isinstance(data, list):
            users = data
            raw_total = None
        else:
            raise MalformedRemoteResponse("Bilibili following response has invalid data")
        items: list[RemoteCandidateIdentity] = []
        for user in users:
            if not isinstance(user, Mapping) or not user.get("mid"):
                raise MalformedRemoteResponse("Bilibili following response contains an invalid user")
            source_creator_id = str(user["mid"])
            items.append(
                RemoteCandidateIdentity(
                    source="bilibili",
                    source_creator_id=source_creator_id,
                    profile_url=f"https://space.bilibili.com/{source_creator_id}/dynamic",
                    display_name=str(user.get("uname") or source_creator_id),
                    username=None,
                    metadata={
                        "group_id": group_id,
                        "face": user.get("face"),
                        "sign": user.get("sign"),
                        "attribute": user.get("attribute"),
                        "mtime": user.get("mtime"),
                        "special": user.get("special"),
                    },
                )
            )
        if raw_total is None:
            has_next = len(items) == min(page_size, 50) and bool(items)
        else:
            try:
                total = int(raw_total)
            except (TypeError, ValueError) as exc:
                raise MalformedRemoteResponse("Bilibili following total is invalid") from exc
            has_next = page * min(page_size, 50) < total
        next_cursor = {"page": page + 1} if has_next else None
        return DiscoveryPage(items=items, next_cursor=next_cursor, done=next_cursor is None)

    def build_download_auth(self, credentials: Mapping[str, Any]) -> DownloadAuthenticationOverride:
        sessdata = required_text(credentials, "SESSDATA", provider="Bilibili")
        return DownloadAuthenticationOverride(
            {"extractor": {"bilibili": {"cookies": {"SESSDATA": sessdata}}}}
        )
