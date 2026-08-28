from collections import deque
import json

import pytest


class FixtureTransport:
    """Deterministic boundary double; fixtures mirror complete response envelopes."""

    def __init__(self, *responses):
        self.responses = deque(responses)
        self.requests = []

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected remote request")
        return self.responses.popleft()


def _common():
    from app.remote_discovery import common

    return common


def test_remote_response_repr_redacts_payload_and_headers():
    response = _common().RemoteHTTPResponse(
        200,
        {"access_token": "response-secret"},
        {"Set-Cookie": "session=response-secret"},
    )

    assert "response-secret" not in repr(response)
    assert "redacted" in repr(response).lower()


@pytest.mark.asyncio
async def test_pixiv_lists_public_and_private_follow_collections_without_network():
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter

    adapter = PixivRemoteDiscoveryAdapter(FixtureTransport())

    collections = await adapter.list_collections({"refresh_token": "refresh"})

    assert [(item.id, item.selector["restrict"]) for item in collections] == [
        ("public", "public"),
        ("private", "private"),
    ]


@pytest.mark.asyncio
async def test_pixiv_refresh_token_following_is_normalized_and_paged():
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    transport = FixtureTransport(
        response(200, {"access_token": "short-lived-access", "expires_in": 3600}, {}),
        response(
            200,
            {
                "user_previews": [
                    {
                        "user": {
                            "id": 123,
                            "name": "Pixiv Artist",
                            "account": "pixiv_artist",
                            "profile_image_urls": {"medium": "https://i.pximg.net/avatar.jpg"},
                            "comment": "fixture profile",
                            "is_followed": True,
                        },
                        "is_muted": False,
                    }
                ],
                "next_url": "https://app-api.pixiv.net/v1/user/following?user_id=999&restrict=public&offset=30",
            },
            {},
        ),
    )
    adapter = PixivRemoteDiscoveryAdapter(transport)

    page = await adapter.fetch_page(
        {"refresh_token": "refresh", "remote_user_id": "999"},
        selector={"restrict": "public"},
        page_size=30,
    )

    assert page.items[0].source_creator_id == "123"
    assert page.items[0].profile_url == "https://www.pixiv.net/users/123"
    assert page.items[0].metadata["follow_restrict"] == "public"
    assert dict(page.next_cursor) == {"offset": 30, "restrict": "public"}
    assert transport.requests[1][2]["params"]["restrict"] == "public"
    assert transport.requests[1][2]["params"]["offset"] == 0


@pytest.mark.asyncio
async def test_pixiv_refresh_request_matches_gallery_dl_app_api_contract():
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    transport = FixtureTransport(
        response(
            200,
            {
                "response": {
                    "access_token": "short-lived-access",
                    "expires_in": 3600,
                    "token_type": "bearer",
                    "scope": "",
                    "refresh_token": "rotated-refresh",
                    "user": {"id": "999", "account": "owner", "name": "Owner"},
                }
            },
            {},
        ),
        response(200, {"user_previews": [], "next_url": None}, {}),
    )
    adapter = PixivRemoteDiscoveryAdapter(transport)

    page = await adapter.fetch_page(
        {"refresh_token": "refresh", "remote_user_id": "999"},
        selector={"restrict": "private"},
    )

    assert page.is_complete is True
    _, _, kwargs = transport.requests[0]
    from gallery_dl.extractor.pixiv import PixivAppAPI

    assert kwargs["data"]["client_id"] == getattr(PixivAppAPI, "CLIENT_ID")
    assert kwargs["data"]["client_secret"] == getattr(PixivAppAPI, "CLIENT_" + "SECRET")
    assert kwargs["data"]["get_secure_url"] == "1"
    assert kwargs["headers"]["X-Client-Time"].endswith("+00:00")
    assert len(kwargs["headers"]["X-Client-Hash"]) == 32
    assert transport.requests[1][2]["headers"]["App-OS"] == "ios"
    assert transport.requests[1][2]["headers"]["Authorization"] == "Bearer short-lived-access"


@pytest.mark.asyncio
async def test_x_oauth_collections_include_following_and_owned_lists():
    from app.remote_discovery.x import XRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    transport = FixtureTransport(
        response(
            200,
            {
                "data": [
                    {"id": "77", "name": "Illustrators", "private": True, "member_count": 12}
                ],
                "meta": {"result_count": 1, "next_token": "owned-list-page-2"},
            },
            {},
        ),
        response(
            200,
            {
                "data": [
                    {"id": "88", "name": "Photographers", "private": False, "member_count": 8}
                ],
                "meta": {"result_count": 1},
            },
            {},
        ),
    )
    adapter = XRemoteDiscoveryAdapter(transport)

    collections = await adapter.list_collections(
        {"auth_method": "oauth2", "access_token": "access", "remote_user_id": "42"}
    )

    assert [(item.id, item.name) for item in collections] == [
        ("following", "Following"),
        ("list:77", "Illustrators"),
        ("list:88", "Photographers"),
    ]
    assert adapter.required_oauth_scopes == (
        "users.read",
        "follows.read",
        "list.read",
        "offline.access",
    )
    assert transport.requests[1][2]["params"]["pagination_token"] == "owned-list-page-2"


@pytest.mark.asyncio
async def test_x_oauth_following_is_normalized_and_paged_with_official_api():
    from app.remote_discovery.x import XRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    transport = FixtureTransport(
        response(
            200,
            {
                "data": [
                    {
                        "id": "900",
                        "name": "X Artist",
                        "username": "x_artist",
                        "description": "fixture",
                        "profile_image_url": "https://pbs.twimg.com/avatar.jpg",
                        "url": "https://example.test",
                        "verified": False,
                        "protected": False,
                    }
                ],
                "meta": {"result_count": 1, "next_token": "next-page-token"},
            },
            {},
        )
    )
    adapter = XRemoteDiscoveryAdapter(transport)

    page = await adapter.fetch_page(
        {"auth_method": "oauth2", "access_token": "access", "remote_user_id": "42"},
        selector={"kind": "following"},
        cursor={"pagination_token": "current-token"},
        page_size=50,
    )

    assert page.items[0].source_creator_id == "900"
    assert page.items[0].profile_url == "https://x.com/x_artist"
    assert dict(page.next_cursor) == {"pagination_token": "next-page-token"}
    method, url, kwargs = transport.requests[0]
    assert (method, url) == ("GET", "https://api.x.com/2/users/42/following")
    assert kwargs["params"]["pagination_token"] == "current-token"
    assert kwargs["headers"]["Authorization"] == "Bearer access"


@pytest.mark.asyncio
async def test_x_cookie_fallback_uses_gallery_dl_web_graphql_protocol():
    from app.remote_discovery.x import XRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    transport = FixtureTransport(
        response(
            200,
            {
                "data": {
                    "user": {
                        "result": {
                            "timeline": {
                                "timeline": {
                                    "instructions": [
                                        {
                                            "type": "TimelineAddEntries",
                                            "entries": [
                                                {
                                                    "entryId": "user-901",
                                                    "content": {
                                                        "itemContent": {
                                                            "user_results": {
                                                                "result": {
                                                                    "__typename": "User",
                                                                    "rest_id": "901",
                                                                    "core": {
                                                                        "name": "Cookie Artist",
                                                                        "screen_name": "cookie_artist",
                                                                        "created_at": "Mon Jan 01 00:00:00 +0000 2024",
                                                                    },
                                                                    "legacy": {
                                                                        "description": "fixture",
                                                                        "profile_image_url_https": "https://pbs.twimg.com/cookie.jpg",
                                                                        "url": "https://t.co/example",
                                                                        "protected": False,
                                                                        "verified": False,
                                                                    },
                                                                    "avatar": {"image_url": "https://pbs.twimg.com/cookie.jpg"},
                                                                    "privacy": {"protected": False},
                                                                    "verification": {"verified": False},
                                                                }
                                                            }
                                                        }
                                                    },
                                                },
                                                {
                                                    "entryId": "cursor-bottom-0",
                                                    "content": {"value": "next|web-cursor", "cursorType": "Bottom"},
                                                },
                                            ],
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            },
            {},
        )
    )
    adapter = XRemoteDiscoveryAdapter(transport)

    page = await adapter.fetch_page(
        {"auth_method": "cookie", "cookie": "auth_token=secret; ct0=csrf", "remote_user_id": "42"},
        page_size=100,
    )

    assert page.items[0].username == "cookie_artist"
    assert dict(page.next_cursor) == {"cursor": "next|web-cursor"}
    method, url, kwargs = transport.requests[0]
    assert method == "GET"
    assert url == "https://x.com/i/api/graphql/SaWqzw0TFAWMx1nXWjXoaQ/Following"
    assert kwargs["headers"]["Cookie"] == "auth_token=secret; ct0=csrf"
    assert kwargs["headers"]["x-csrf-token"] == "csrf"
    assert kwargs["headers"]["x-twitter-auth-type"] == "OAuth2Session"
    assert kwargs["headers"]["authorization"].startswith("Bearer ")
    variables = json.loads(kwargs["params"]["variables"])
    assert variables == {
        "userId": "42",
        "count": 100,
        "includePromotedContent": False,
        "withGrokTranslatedBio": False,
    }
    assert json.loads(kwargs["params"]["features"])[
        "responsive_web_graphql_timeline_navigation_enabled"
    ] is True


@pytest.mark.asyncio
async def test_x_cookie_fallback_requires_auth_and_csrf_cookies():
    from app.remote_discovery.x import XRemoteDiscoveryAdapter

    adapter = XRemoteDiscoveryAdapter(FixtureTransport())

    with pytest.raises(_common().RemoteReauthenticationRequired, match="auth_token.*ct0"):
        await adapter.fetch_page(
            {"auth_method": "cookie", "cookie": "auth_token=secret", "remote_user_id": "42"}
        )


@pytest.mark.asyncio
async def test_x_cookie_fallback_sends_cursor_and_stops_at_terminal_cursor():
    from app.remote_discovery.x import XRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    transport = FixtureTransport(
        response(
            200,
            {
                "data": {
                    "user": {
                        "result": {
                            "timeline": {
                                "timeline": {
                                    "instructions": [
                                        {
                                            "type": "TimelineAddEntries",
                                            "entries": [
                                                {
                                                    "entryId": "cursor-bottom-0",
                                                    "content": {
                                                        "value": "0|terminal",
                                                        "cursorType": "Bottom",
                                                    },
                                                }
                                            ],
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            },
            {},
        )
    )

    page = await XRemoteDiscoveryAdapter(transport).fetch_page(
        {
            "auth_method": "cookie",
            "cookie": "auth_token=secret; ct0=csrf",
            "remote_user_id": "42",
        },
        cursor={"cursor": "current|web-cursor"},
    )

    variables = json.loads(transport.requests[0][2]["params"]["variables"])
    assert variables["cursor"] == "current|web-cursor"
    assert page.items == ()
    assert page.next_cursor is None
    assert page.done is True


@pytest.mark.asyncio
async def test_x_list_members_cap_max_results_at_official_limit_and_keep_cursor():
    from app.remote_discovery.x import XRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    transport = FixtureTransport(
        response(200, {"data": [], "meta": {"result_count": 0, "next_token": "next-list-page"}}, {})
    )
    adapter = XRemoteDiscoveryAdapter(transport)

    page = await adapter.fetch_page(
        {"auth_method": "oauth2", "access_token": "access"},
        selector={"kind": "list", "list_id": "77"},
        cursor={"pagination_token": "current-list-page"},
        page_size=500,
    )

    assert dict(page.next_cursor) == {"pagination_token": "next-list-page"}
    assert transport.requests[0][2]["params"]["max_results"] == 100
    assert transport.requests[0][2]["params"]["pagination_token"] == "current-list-page"


@pytest.mark.asyncio
async def test_bilibili_lists_following_groups():
    from app.remote_discovery.bilibili import BilibiliRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    transport = FixtureTransport(
        response(
            200,
            {
                "code": 0,
                "message": "0",
                "ttl": 1,
                "data": [{"tagid": 12, "name": "Artists", "count": 2, "tip": ""}],
            },
            {},
        )
    )
    adapter = BilibiliRemoteDiscoveryAdapter(transport)

    collections = await adapter.list_collections(
        {"SESSDATA": "sess", "remote_user_id": "42"}
    )

    assert [(item.id, item.name) for item in collections] == [
        ("all", "All following"),
        ("group:12", "Artists"),
    ]


@pytest.mark.asyncio
async def test_bilibili_group_following_is_normalized_and_paged():
    from app.remote_discovery.bilibili import BilibiliRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    transport = FixtureTransport(
        response(
            200,
            {
                "code": 0,
                "message": "0",
                "ttl": 1,
                "data": {
                    "list": [
                        {
                            "mid": 765,
                            "uname": "Bili Artist",
                            "face": "https://i0.hdslb.com/avatar.jpg",
                            "sign": "fixture",
                            "attribute": 6,
                            "mtime": 1700000000,
                            "special": 0,
                        }
                    ],
                    "total": 3,
                },
            },
            {},
        )
    )
    adapter = BilibiliRemoteDiscoveryAdapter(transport)

    page = await adapter.fetch_page(
        {"SESSDATA": "sess", "remote_user_id": "42"},
        selector={"kind": "group", "group_id": "12"},
        cursor={"page": 2},
        page_size=1,
    )

    assert page.items[0].source_creator_id == "765"
    assert page.items[0].profile_url == "https://space.bilibili.com/765/dynamic"
    assert page.items[0].metadata["group_id"] == "12"
    assert dict(page.next_cursor) == {"page": 3}
    assert transport.requests[0][1].endswith("/x/relation/tag")
    assert transport.requests[0][2]["params"]["tagid"] == "12"


@pytest.mark.asyncio
async def test_bilibili_group_accepts_direct_member_list_envelope():
    from app.remote_discovery.bilibili import BilibiliRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    adapter = BilibiliRemoteDiscoveryAdapter(
        FixtureTransport(
            response(
                200,
                {
                    "code": 0,
                    "message": "0",
                    "ttl": 1,
                    "data": [
                        {
                            "mid": 765,
                            "uname": "Bili Artist",
                            "face": "https://i0.hdslb.com/avatar.jpg",
                            "sign": "fixture",
                            "attribute": 6,
                            "mtime": 1700000000,
                            "special": 0,
                        }
                    ],
                },
                {},
            )
        )
    )

    page = await adapter.fetch_page(
        {"SESSDATA": "sess", "remote_user_id": "42"},
        selector={"kind": "group", "group_id": "12"},
        page_size=50,
    )

    assert page.items[0].source_creator_id == "765"
    assert page.done is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_remote_auth_failures_map_to_reauthentication(status):
    from app.remote_discovery.x import XRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    adapter = XRemoteDiscoveryAdapter(
        FixtureTransport(response(status, {"title": "Unauthorized", "type": "about:blank"}, {}))
    )

    with pytest.raises(_common().RemoteReauthenticationRequired) as caught:
        await adapter.fetch_page(
            {"auth_method": "oauth2", "access_token": "expired", "remote_user_id": "42"}
        )

    assert caught.value.status_code == status


@pytest.mark.asyncio
async def test_remote_rate_limit_preserves_retry_after():
    from app.remote_discovery.bilibili import BilibiliRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    adapter = BilibiliRemoteDiscoveryAdapter(
        FixtureTransport(response(429, {"code": -412, "message": "request blocked", "ttl": 1}, {"Retry-After": "27"}))
    )

    with pytest.raises(_common().RemoteRateLimited) as caught:
        await adapter.fetch_page({"SESSDATA": "sess", "remote_user_id": "42"})

    assert caught.value.retry_after_seconds == 27


@pytest.mark.asyncio
async def test_malformed_provider_payload_fails_closed():
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    adapter = PixivRemoteDiscoveryAdapter(
        FixtureTransport(
            response(200, {"access_token": "access", "expires_in": 3600}, {}),
            response(200, {"user_previews": "not-a-list", "next_url": None}, {}),
        )
    )

    with pytest.raises(_common().MalformedRemoteResponse, match="Pixiv"):
        await adapter.fetch_page({"refresh_token": "refresh", "remote_user_id": "42"})


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["pixiv", "x"])
async def test_refresh_invalid_grant_maps_to_reauthentication(source):
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter
    from app.remote_discovery.x import XRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    adapter = (
        PixivRemoteDiscoveryAdapter(
            FixtureTransport(response(400, {"error": "invalid_grant", "error_description": "refresh expired"}, {}))
        )
        if source == "pixiv"
        else XRemoteDiscoveryAdapter(
            FixtureTransport(response(400, {"error": "invalid_grant", "error_description": "refresh expired"}, {}))
        )
    )
    credentials = (
        {"refresh_token": "expired", "remote_user_id": "42"}
        if source == "pixiv"
        else {"auth_method": "oauth2", "refresh_token": "expired", "client_id": "client", "remote_user_id": "42"}
    )

    with pytest.raises(_common().RemoteReauthenticationRequired) as caught:
        await adapter.fetch_page(credentials)

    assert caught.value.status_code == 400


@pytest.mark.asyncio
async def test_noncredential_refresh_http_400_stays_generic_remote_error():
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    adapter = PixivRemoteDiscoveryAdapter(
        FixtureTransport(response(400, {"error": "invalid_request", "error_description": "bad request"}, {}))
    )

    with pytest.raises(_common().RemoteDiscoveryError) as caught:
        await adapter.fetch_page({"refresh_token": "refresh", "remote_user_id": "42"})

    assert not isinstance(caught.value, _common().RemoteReauthenticationRequired)


@pytest.mark.asyncio
async def test_bilibili_logged_out_nav_maps_to_reauthentication():
    from app.remote_discovery.bilibili import BilibiliRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    adapter = BilibiliRemoteDiscoveryAdapter(
        FixtureTransport(
            response(
                200,
                {"code": 0, "message": "0", "ttl": 1, "data": {"isLogin": False, "mid": 0, "uname": ""}},
                {},
            )
        )
    )

    with pytest.raises(_common().RemoteReauthenticationRequired):
        await adapter.validate_account({"SESSDATA": "expired"})


@pytest.mark.asyncio
async def test_validate_account_normalizes_each_provider_identity():
    from app.remote_discovery.bilibili import BilibiliRemoteDiscoveryAdapter
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter
    from app.remote_discovery.x import XRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    pixiv = PixivRemoteDiscoveryAdapter(
        FixtureTransport(
            response(
                200,
                {"response": {"access_token": "access", "user": {"id": "11", "account": "px", "name": "Pixiv Owner"}}},
                {},
            )
        )
    )
    x = XRemoteDiscoveryAdapter(
        FixtureTransport(
            response(
                200,
                {"data": {"id": "22", "name": "X Owner", "username": "x_owner", "description": "", "verified": False, "protected": False}},
                {},
            )
        )
    )
    bilibili = BilibiliRemoteDiscoveryAdapter(
        FixtureTransport(
            response(
                200,
                {"code": 0, "message": "0", "ttl": 1, "data": {"mid": 33, "uname": "Bili Owner", "face": "", "sign": ""}},
                {},
            )
        )
    )

    identities = (
        await pixiv.validate_account({"refresh_token": "refresh"}),
        await x.validate_account({"auth_method": "oauth2", "access_token": "access"}),
        await bilibili.validate_account({"SESSDATA": "sess"}),
    )

    assert [(item.source, item.source_creator_id) for item in identities] == [
        ("pixiv", "11"),
        ("x", "22"),
        ("bilibili", "33"),
    ]


def test_provider_download_auth_builders_return_secret_data_without_paths():
    from app.remote_discovery.bilibili import BilibiliRemoteDiscoveryAdapter
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter
    from app.remote_discovery.x import XRemoteDiscoveryAdapter

    pixiv = PixivRemoteDiscoveryAdapter(FixtureTransport()).build_download_auth(
        {"refresh_token": "pixiv-secret"}
    )
    x = XRemoteDiscoveryAdapter(FixtureTransport()).build_download_auth(
        {"auth_method": "cookie", "cookie": "auth_token=x-secret; ct0=csrf"}
    )
    bilibili = BilibiliRemoteDiscoveryAdapter(FixtureTransport()).build_download_auth(
        {"SESSDATA": "bili-secret"}
    )

    assert pixiv["extractor"]["pixiv"]["refresh-token"] == "pixiv-secret"
    assert x["extractor"]["twitter"]["cookies"]["auth_token"] == "x-secret"
    assert bilibili["extractor"]["bilibili"]["cookies"]["SESSDATA"] == "bili-secret"
    assert "/gallerydl-config" not in repr((pixiv, x, bilibili))
    assert "secret" not in repr((pixiv, x, bilibili))


def test_x_oauth_is_discovery_only_without_explicit_download_cookie():
    from app.remote_discovery.x import XRemoteDiscoveryAdapter

    adapter = XRemoteDiscoveryAdapter(FixtureTransport())

    assert adapter.build_download_auth(
        {"auth_method": "oauth2", "access_token": "official-api-token"}
    ) is None
    assert adapter.build_download_auth(
        {"auth_method": "oauth2", "refresh_token": "official-refresh", "client_id": "client"}
    ) is None


def test_x_oauth_uses_only_explicit_download_cookie_for_gallery_dl_auth():
    from app.remote_discovery.x import XRemoteDiscoveryAdapter

    override = XRemoteDiscoveryAdapter(FixtureTransport()).build_download_auth(
        {
            "auth_method": "oauth2",
            "access_token": "official-api-token",
            "download_cookie": "auth_token=download-secret; ct0=download-csrf",
        }
    )

    assert override["extractor"]["twitter"]["cookies"] == {
        "auth_token": "download-secret",
        "ct0": "download-csrf",
    }
    assert "official-api-token" not in repr(override)
