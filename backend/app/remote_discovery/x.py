"""X remote-follow discovery via OAuth 2 official API or cookie fallback."""

from __future__ import annotations

from collections.abc import Mapping
from http.cookies import SimpleCookie
import json
from typing import Any

from app.remote_discovery.common import (
    HttpxRemoteTransport,
    MalformedRemoteResponse,
    RemoteHTTPTransport,
    RemoteReauthenticationRequired,
    checked_oauth_token_payload,
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
from app.remote_discovery.x_transaction import (
    GalleryDlXTransactionIdProvider,
    XTransactionIdProvider,
)
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
    WEB_API_BASE = "https://x.com/i/api"
    COOKIE_FOLLOWING_ENDPOINT = "/graphql/SaWqzw0TFAWMx1nXWjXoaQ/Following"
    # Public X web-client protocol token characterized against gallery-dl 1.32.9.
    # It authenticates the client application, not a user account.
    WEB_BEARER_TOKEN = (
        "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejR"
        "COuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu"
        "4FA33AGWWjCpTnA"
    )
    WEB_PAGINATION_FEATURES = {
        "rweb_video_screen_enabled": False,
        "payments_enabled": False,
        "rweb_xchat_enabled": False,
        "profile_label_improvements_pcf_label_in_post_enabled": True,
        "rweb_tipjar_consumption_enabled": True,
        "verified_phone_label_enabled": False,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "premium_content_api_read_enabled": False,
        "communities_web_enable_tweet_community_results_fetch": True,
        "c9s_tweet_anatomy_moderator_badge_enabled": True,
        "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
        "responsive_web_grok_analyze_post_followups_enabled": True,
        "responsive_web_jetfuel_frame": True,
        "responsive_web_grok_share_attachment_enabled": True,
        "articles_preview_enabled": True,
        "responsive_web_edit_tweet_api_enabled": True,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
        "view_counts_everywhere_api_enabled": True,
        "longform_notetweets_consumption_enabled": True,
        "responsive_web_twitter_article_tweet_consumption_enabled": True,
        "tweet_awards_web_tipping_enabled": False,
        "responsive_web_grok_show_grok_translated_post": False,
        "responsive_web_grok_analysis_button_from_backend": True,
        "creator_subscriptions_quote_tweet_preview_enabled": False,
        "freedom_of_speech_not_reach_fetch_enabled": True,
        "standardized_nudges_misinfo": True,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
        "longform_notetweets_rich_text_read_enabled": True,
        "longform_notetweets_inline_media_enabled": True,
        "responsive_web_grok_image_annotation_enabled": True,
        "responsive_web_grok_imagine_annotation_enabled": True,
        "responsive_web_grok_community_note_auto_translation_is_enabled": False,
        "responsive_web_enhance_cards_enabled": False,
    }

    def __init__(
        self,
        transport: RemoteHTTPTransport | None = None,
        *,
        transaction_id_provider: XTransactionIdProvider | None = None,
    ):
        self.transport = transport or HttpxRemoteTransport()
        self.transaction_id_provider = (
            transaction_id_provider or GalleryDlXTransactionIdProvider()
        )

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
            url = "https://api.x.com/1.1/account/verify_credentials.json"
            headers = await self._authenticated_web_headers(
                credentials,
                method="GET",
                url=url,
            )
            response = await self.transport.request(
                "GET",
                url,
                headers=headers,
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
        payload = checked_oauth_token_payload(response, provider="X")
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
        collections = [RemoteCollection("following", "Following", {"kind": "following"})]
        pagination_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            params: dict[str, Any] = {
                "max_results": 100,
                "list.fields": "id,name,private,member_count",
            }
            if pagination_token:
                params["pagination_token"] = pagination_token
            response = await self.transport.request(
                "GET",
                f"{self.API_BASE}/users/{user_id}/owned_lists",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
            payload = checked_payload(response, provider="X")
            data = payload.get("data", [])
            meta = payload.get("meta", {})
            if not isinstance(data, list) or not isinstance(meta, Mapping):
                raise MalformedRemoteResponse("X owned lists response has invalid data or meta")
            for item in data:
                if not isinstance(item, Mapping) or not item.get("id") or not item.get("name"):
                    raise MalformedRemoteResponse(
                        "X owned lists response contains an invalid list"
                    )
                list_id = str(item["id"])
                collections.append(
                    RemoteCollection(
                        f"list:{list_id}",
                        str(item["name"]),
                        {
                            "kind": "list",
                            "list_id": list_id,
                            "private": bool(item.get("private")),
                        },
                    )
                )
            raw_next_token = meta.get("next_token")
            if not raw_next_token:
                break
            pagination_token = str(raw_next_token)
            if pagination_token in seen_tokens:
                raise MalformedRemoteResponse("X owned lists response repeated a pagination token")
            seen_tokens.add(pagination_token)
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
            "user.fields": "id,name,username,description,profile_image_url,url,verified,protected",
        }
        if cursor and cursor.get("pagination_token"):
            params["pagination_token"] = str(cursor["pagination_token"])
        if collection_id == "following":
            user_id = required_text(credentials, "remote_user_id", provider="X")
            url = f"{self.API_BASE}/users/{user_id}/following"
            params["max_results"] = min(max(page_size, 1), 1000)
        elif collection_id.startswith("list:") and collection_id[5:]:
            url = f"{self.API_BASE}/lists/{collection_id[5:]}/members"
            params["max_results"] = min(max(page_size, 1), 100)
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
        user_id = required_text(credentials, "remote_user_id", provider="X")
        variables: dict[str, Any] = {
            "userId": user_id,
            "count": min(page_size, 100),
            "includePromotedContent": False,
            "withGrokTranslatedBio": False,
        }
        if cursor and cursor.get("cursor"):
            variables["cursor"] = str(cursor["cursor"])
        url = f"{self.WEB_API_BASE}{self.COOKIE_FOLLOWING_ENDPOINT}"
        headers = await self._authenticated_web_headers(
            credentials,
            method="GET",
            url=url,
        )
        response = await self.transport.request(
            "GET",
            url,
            headers=headers,
            params={
                "variables": json.dumps(variables, separators=(",", ":")),
                "features": json.dumps(
                    self.WEB_PAGINATION_FEATURES,
                    separators=(",", ":"),
                ),
            },
        )
        payload = checked_payload(response, provider="X")
        items, next_cursor = self._parse_web_following(payload)
        terminal = not next_cursor or next_cursor.startswith(("-1|", "0|"))
        return DiscoveryPage(
            items=items,
            next_cursor=None if terminal else {"cursor": next_cursor},
            done=terminal,
        )

    @staticmethod
    def _cookie_values(raw_cookie: str) -> dict[str, str]:
        parsed = SimpleCookie()
        parsed.load(raw_cookie)
        return {key: morsel.value for key, morsel in parsed.items()}

    def _web_headers(self, credentials: Mapping[str, Any]) -> dict[str, str]:
        raw_cookie = required_text(credentials, "cookie", provider="X")
        cookies = self._cookie_values(raw_cookie)
        if not cookies.get("auth_token") or not cookies.get("ct0"):
            raise RemoteReauthenticationRequired(
                401,
                "X cookie credentials require auth_token and ct0",
            )
        return {
            "Accept": "*/*",
            "Referer": "https://x.com/",
            "content-type": "application/json",
            "Cookie": raw_cookie,
            "x-twitter-auth-type": "OAuth2Session",
            "x-csrf-token": cookies["ct0"],
            "x-twitter-client-language": "en",
            "x-twitter-active-user": "yes",
            "authorization": f"Bearer {self.WEB_BEARER_TOKEN}",
        }

    async def _authenticated_web_headers(
        self,
        credentials: Mapping[str, Any],
        *,
        method: str,
        url: str,
    ) -> dict[str, str]:
        headers = self._web_headers(credentials)
        headers["x-client-transaction-id"] = await self.transaction_id_provider.generate(
            method,
            url,
            cookie=headers["Cookie"],
        )
        return headers

    @classmethod
    def _parse_web_following(
        cls,
        payload: Mapping[str, Any],
    ) -> tuple[list[RemoteCandidateIdentity], str | None]:
        try:
            instructions = payload["data"]["user"]["result"]["timeline"]["timeline"][
                "instructions"
            ]
        except (KeyError, TypeError) as exc:
            raise MalformedRemoteResponse(
                "X web following response has invalid timeline data"
            ) from exc
        if not isinstance(instructions, list):
            raise MalformedRemoteResponse("X web following response has invalid instructions")

        items: list[RemoteCandidateIdentity] = []
        next_cursor: str | None = None
        for instruction in instructions:
            if not isinstance(instruction, Mapping):
                raise MalformedRemoteResponse(
                    "X web following response contains an invalid instruction"
                )
            entries: Any = None
            if instruction.get("type") == "TimelineAddEntries":
                entries = instruction.get("entries")
            elif instruction.get("type") == "TimelineReplaceEntry":
                entry = instruction.get("entry")
                entries = [entry] if isinstance(entry, Mapping) else None
            if entries is None:
                continue
            if not isinstance(entries, list):
                raise MalformedRemoteResponse(
                    "X web following response contains invalid timeline entries"
                )
            for entry in entries:
                if not isinstance(entry, Mapping):
                    raise MalformedRemoteResponse(
                        "X web following response contains an invalid timeline entry"
                    )
                entry_id = str(entry.get("entryId") or "")
                content = entry.get("content")
                if not isinstance(content, Mapping):
                    raise MalformedRemoteResponse(
                        "X web following response contains invalid entry content"
                    )
                if entry_id.startswith("user-"):
                    try:
                        user = content["itemContent"]["user_results"]["result"]
                    except (KeyError, TypeError) as exc:
                        raise MalformedRemoteResponse(
                            "X web following response contains invalid user data"
                        ) from exc
                    items.append(cls._web_candidate(user))
                elif entry_id.startswith("cursor-bottom-"):
                    value = content.get("value")
                    if not isinstance(value, str) or not value:
                        raise MalformedRemoteResponse(
                            "X web following response contains an invalid cursor"
                        )
                    next_cursor = value
        return items, next_cursor

    @classmethod
    def _web_candidate(cls, user: Any) -> RemoteCandidateIdentity:
        if not isinstance(user, Mapping) or not user.get("rest_id"):
            raise MalformedRemoteResponse("X web following user is missing an id")
        core = user.get("core")
        legacy = user.get("legacy")
        if not isinstance(core, Mapping) or not isinstance(legacy, Mapping):
            raise MalformedRemoteResponse("X web following user has invalid profile data")
        privacy = user.get("privacy")
        verification = user.get("verification")
        avatar = user.get("avatar")
        normalized = {
            "id": user["rest_id"],
            "name": core.get("name"),
            "username": core.get("screen_name"),
            "description": legacy.get("description"),
            "profile_image_url": (
                avatar.get("image_url")
                if isinstance(avatar, Mapping)
                else legacy.get("profile_image_url_https")
            ),
            "url": legacy.get("url"),
            "protected": (
                privacy.get("protected")
                if isinstance(privacy, Mapping)
                else legacy.get("protected")
            ),
            "verified": (
                verification.get("verified")
                if isinstance(verification, Mapping)
                else legacy.get("verified")
            ),
        }
        return cls._candidate(normalized, auth_method="cookie")

    def build_download_auth(
        self,
        credentials: Mapping[str, Any],
    ) -> DownloadAuthenticationOverride | None:
        if self._auth_method(credentials) == "oauth2":
            if "download_cookie" not in credentials:
                return None
            raw_cookie = required_text(credentials, "download_cookie", provider="X")
        else:
            raw_cookie = required_text(credentials, "cookie", provider="X")
        cookies = self._cookie_values(raw_cookie)
        if not cookies.get("auth_token") or not cookies.get("ct0"):
            raise RemoteReauthenticationRequired(
                401,
                "X download cookies require auth_token and ct0",
            )
        return DownloadAuthenticationOverride({"extractor": {"twitter": {"cookies": cookies}}})
