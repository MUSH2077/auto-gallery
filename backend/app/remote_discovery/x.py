"""X remote-follow discovery via OAuth 2 official API or cookie fallback."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
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
    RemoteCandidateEvidence,
    RemoteCandidateIdentity,
    RemoteCollection,
    RemoteDiscoveryAdapter,
)
from app.remote_discovery.evidence import (
    art_focused_bio,
    expanded_profile_links,
    has_recent_visual_post,
    supported_profile_links,
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
        "tweet.read",
        "users.read",
        "follows.read",
        "list.read",
        "offline.access",
    )
    API_BASE = "https://api.x.com/2"
    WEB_API_BASE = "https://x.com/i/api"
    COOKIE_FOLLOWING_ENDPOINT = "/graphql/SaWqzw0TFAWMx1nXWjXoaQ/Following"
    COOKIE_USER_MEDIA_ENDPOINT = "/graphql/jCRhbOzdgOHp6u9H4g2tEg/UserMedia"
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
            response = await self._oauth_request(
                credentials,
                "GET",
                f"{self.API_BASE}/users/me",
                params={"user.fields": "id,name,username,description,entities,profile_image_url,url,verified,protected"},
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
        rotated = await self._refresh_oauth_tokens(credentials)
        token = rotated.get("access_token")
        if not isinstance(token, str) or not token:
            raise MalformedRemoteResponse("X token response is missing access_token")
        return token

    async def _refresh_oauth_tokens(
        self,
        credentials: Mapping[str, Any],
    ) -> dict[str, Any]:
        async def request_rotation(current: Mapping[str, Any]) -> Mapping[str, Any]:
            refresh_token = required_text(current, "refresh_token", provider="X")
            client_id = required_text(current, "client_id", provider="X")
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
            access_token = payload.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise MalformedRemoteResponse("X token response is missing access_token")
            rotated = dict(current)
            rotated["access_token"] = access_token
            refresh = payload.get("refresh_token")
            if isinstance(refresh, str) and refresh:
                rotated["refresh_token"] = refresh
            expected_remote_user_id = current.get("remote_user_id")
            if isinstance(expected_remote_user_id, str) and expected_remote_user_id:
                identity_response = await self.transport.request(
                    "GET",
                    f"{self.API_BASE}/users/me",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={"user.fields": "id"},
                )
                identity_payload = checked_payload(identity_response, provider="X")
                identity = identity_payload.get("data")
                if (
                    not isinstance(identity, Mapping)
                    or str(identity.get("id") or "") != expected_remote_user_id
                ):
                    raise RemoteReauthenticationRequired(
                        401,
                        "X OAuth account identity changed",
                    )
            return rotated

        coordinator = getattr(credentials, "rotate_oauth_tokens", None)
        if callable(coordinator):
            return dict(await coordinator(request_rotation))
        rotated = dict(await request_rotation(credentials))
        if isinstance(credentials, dict):
            credentials.update(rotated)
        return rotated

    async def _oauth_request(
        self,
        credentials: Mapping[str, Any],
        method: str,
        url: str,
        **kwargs: Any,
    ):
        token = await self._oauth_access_token(credentials)
        response = await self.transport.request(
            method,
            url,
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )
        if response.status_code != 401:
            return response
        # Only official OAuth refreshes. Cookie mode never crosses this helper.
        rotated = await self._refresh_oauth_tokens(credentials)
        return await self.transport.request(
            method,
            url,
            headers={"Authorization": f"Bearer {rotated['access_token']}"},
            **kwargs,
        )

    async def list_collections(self, credentials: Mapping[str, Any]) -> tuple[RemoteCollection, ...]:
        if self._auth_method(credentials) == "cookie":
            required_text(credentials, "cookie", provider="X")
            return (RemoteCollection("following", "Following", {"kind": "following"}),)
        user_id = required_text(credentials, "remote_user_id", provider="X")
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
            response = await self._oauth_request(
                credentials,
                "GET",
                f"{self.API_BASE}/users/{user_id}/owned_lists",
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
    def _profile_link_values(user: Mapping[str, Any]) -> tuple[str, ...]:
        values: list[str] = []
        direct = user.get("url")
        if isinstance(direct, str):
            values.append(direct)
        entities = user.get("entities")
        if isinstance(entities, Mapping):
            for section_name in ("url", "description"):
                section = entities.get(section_name)
                if not isinstance(section, Mapping):
                    continue
                urls = section.get("urls")
                if not isinstance(urls, list):
                    continue
                for item in urls:
                    if not isinstance(item, Mapping):
                        continue
                    value = item.get("unwound_url") or item.get("expanded_url")
                    if isinstance(value, str):
                        values.append(value)
        return expanded_profile_links(values)

    @classmethod
    def _profile_links(cls, user: Mapping[str, Any]) -> tuple[str, ...]:
        return supported_profile_links(cls._profile_link_values(user), source="x")

    @classmethod
    def _candidate(cls, user: Mapping[str, Any], *, auth_method: str) -> RemoteCandidateIdentity:
        source_creator_id = str(user.get("id") or user.get("id_str") or "")
        username = str(user.get("username") or user.get("screen_name") or "")
        if not source_creator_id or not username:
            raise MalformedRemoteResponse("X following user is missing id or username")
        description = user.get("description")
        links = cls._profile_links(user)
        expanded_links = cls._profile_link_values(user)
        return RemoteCandidateIdentity(
            source="x",
            source_creator_id=source_creator_id,
            profile_url=f"https://x.com/{username}",
            display_name=str(user.get("name") or username),
            username=username,
            metadata={
                "auth_method": auth_method,
                "description": description,
                "profile_image_url": user.get("profile_image_url") or user.get("profile_image_url_https"),
                "url": user.get("url"),
                "verified": user.get("verified"),
                "protected": user.get("protected"),
                "art_focused_bio": art_focused_bio(description),
                "supported_links": links,
                "expanded_links": expanded_links,
                "supported_site_link": bool(links),
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
        params: dict[str, Any] = {
            "user.fields": "id,name,username,description,entities,profile_image_url,url,verified,protected",
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
        response = await self._oauth_request(
            credentials,
            "GET",
            url,
            params=params,
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
            "entities": legacy.get("entities"),
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

    async def enrich_candidate(
        self,
        credentials: Mapping[str, Any],
        identity: RemoteCandidateIdentity,
    ) -> RemoteCandidateEvidence:
        if identity.source != self.source:
            raise ValueError("candidate source does not match X adapter")
        metadata = dict(identity.metadata)
        metadata["art_focused_bio"] = art_focused_bio(metadata.get("description"))
        links = supported_profile_links(
            metadata.get("supported_links") or (),
            source="x",
        )
        metadata["supported_links"] = links
        metadata["supported_site_link"] = bool(links)
        if self._auth_method(credentials) == "oauth2":
            posts = await self._oauth_recent_media(credentials, identity.source_creator_id)
        else:
            posts = await self._cookie_recent_media(credentials, identity.source_creator_id)
        metadata["recent_visual_post"] = has_recent_visual_post(posts)
        return RemoteCandidateEvidence(
            source="x",
            source_creator_id=identity.source_creator_id,
            metadata=metadata,
        )

    async def _oauth_recent_media(
        self,
        credentials: Mapping[str, Any],
        source_creator_id: str,
    ) -> list[dict[str, Any]]:
        response = await self._oauth_request(
            credentials,
            "GET",
            f"{self.API_BASE}/users/{source_creator_id}/tweets",
            params={
                "max_results": 5,
                "exclude": "retweets,replies",
                "tweet.fields": "attachments,created_at",
                "expansions": "attachments.media_keys",
                "media.fields": "media_key,type",
            },
        )
        payload = checked_payload(response, provider="X")
        data = payload.get("data", [])
        includes = payload.get("includes", {})
        if not isinstance(data, list) or not isinstance(includes, Mapping):
            raise MalformedRemoteResponse("X user posts response has invalid data")
        media = includes.get("media", [])
        if not isinstance(media, list):
            raise MalformedRemoteResponse("X user posts response has invalid media")
        visual_keys = {
            str(item.get("media_key"))
            for item in media
            if isinstance(item, Mapping)
            and item.get("media_key")
            and item.get("type") in {"photo", "video", "animated_gif"}
        }
        posts: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, Mapping):
                raise MalformedRemoteResponse("X user posts response contains an invalid post")
            attachments = item.get("attachments")
            keys = attachments.get("media_keys", []) if isinstance(attachments, Mapping) else []
            if not isinstance(keys, list):
                raise MalformedRemoteResponse("X post attachments are invalid")
            posts.append(
                {
                    "created_at": item.get("created_at"),
                    "has_visual_media": any(str(key) in visual_keys for key in keys),
                }
            )
        return posts

    async def _cookie_recent_media(
        self,
        credentials: Mapping[str, Any],
        source_creator_id: str,
    ) -> list[dict[str, Any]]:
        url = f"{self.WEB_API_BASE}{self.COOKIE_USER_MEDIA_ENDPOINT}"
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
                "variables": json.dumps(
                    {
                        "userId": source_creator_id,
                        "count": 5,
                        "includePromotedContent": False,
                        "withClientEventToken": False,
                        "withBirdwatchNotes": False,
                        "withVoice": True,
                    },
                    separators=(",", ":"),
                ),
                "features": json.dumps(self.WEB_PAGINATION_FEATURES, separators=(",", ":")),
                "fieldToggles": json.dumps(
                    {"withArticlePlainText": False}, separators=(",", ":")
                ),
            },
        )
        payload = checked_payload(response, provider="X")
        return self._parse_web_media(payload)

    @staticmethod
    def _parse_web_media(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            instructions = payload["data"]["user"]["result"]["timeline"]["timeline"]["instructions"]
        except (KeyError, TypeError) as exc:
            raise MalformedRemoteResponse("X web media response has invalid timeline data") from exc
        if not isinstance(instructions, list):
            raise MalformedRemoteResponse("X web media response has invalid instructions")
        posts: list[dict[str, Any]] = []
        for instruction in instructions:
            if not isinstance(instruction, Mapping):
                raise MalformedRemoteResponse("X web media response contains an invalid instruction")
            entries = instruction.get("entries") if instruction.get("type") == "TimelineAddEntries" else []
            if not isinstance(entries, list):
                raise MalformedRemoteResponse("X web media response contains invalid entries")
            for entry in entries:
                if not isinstance(entry, Mapping) or not str(entry.get("entryId") or "").startswith("tweet-"):
                    continue
                try:
                    tweet = entry["content"]["itemContent"]["tweet_results"]["result"]
                    legacy = tweet["legacy"]
                except (KeyError, TypeError) as exc:
                    raise MalformedRemoteResponse("X web media response contains invalid post data") from exc
                media = legacy.get("extended_entities", {}).get("media", [])
                if not isinstance(media, list):
                    raise MalformedRemoteResponse("X web media response contains invalid post media")
                try:
                    created_at = datetime.strptime(
                        str(legacy.get("created_at") or ""),
                        "%a %b %d %H:%M:%S %z %Y",
                    )
                except ValueError as exc:
                    raise MalformedRemoteResponse("X web media response contains invalid post time") from exc
                posts.append(
                    {
                        "created_at": created_at,
                        "has_visual_media": any(
                            isinstance(item, Mapping)
                            and item.get("type") in {"photo", "video", "animated_gif"}
                            for item in media
                        ),
                    }
                )
        return posts

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
