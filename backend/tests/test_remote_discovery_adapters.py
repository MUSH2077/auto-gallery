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


class FixtureTransactionIdProvider:
    def __init__(self):
        self.calls = []

    async def generate(self, method, url, *, cookie):
        self.calls.append((method, url, cookie))
        return f"fixture-transaction-{len(self.calls)}"


def _common():
    from app.remote_discovery import common

    return common


def _pixiv_illust(
    work_id: int,
    *,
    user_id: int = 123,
    work_type: str = "illust",
    page_count: int = 1,
    x_restrict: int = 0,
):
    meta_pages = [
        {
            "image_urls": {
                "square_medium": f"https://i.pximg.net/c/360x360/{work_id}_p{page}.jpg",
                "medium": f"https://i.pximg.net/c/540x540/{work_id}_p{page}.jpg",
                "large": f"https://i.pximg.net/img-master/{work_id}_p{page}.jpg",
            }
        }
        for page in range(page_count)
    ]
    return {
        "id": work_id,
        "title": f"Fixture {work_id}",
        "type": work_type,
        "image_urls": {
            "square_medium": f"https://i.pximg.net/c/360x360/{work_id}_p0.jpg",
            "medium": f"https://i.pximg.net/c/540x540/{work_id}_p0.jpg",
            "large": f"https://i.pximg.net/img-master/{work_id}_p0.jpg",
        },
        "caption": "fixture caption",
        "restrict": 0,
        "user": {
            "id": user_id,
            "name": "Pixiv Artist",
            "account": "pixiv_artist",
            "profile_image_urls": {"medium": "https://i.pximg.net/avatar.jpg"},
            "comment": "fixture profile",
            "is_followed": True,
        },
        "tags": [],
        "tools": [],
        "create_date": "2026-08-30T10:20:30+09:00",
        "page_count": page_count,
        "width": 1200,
        "height": 900,
        "sanity_level": 2,
        "x_restrict": x_restrict,
        "series": None,
        "meta_single_page": {},
        "meta_pages": meta_pages if page_count > 1 else [],
        "total_view": 100,
        "total_bookmarks": 10,
        "is_bookmarked": False,
        "visible": True,
        "is_muted": False,
        "illust_ai_type": 1,
        "illust_book_style": 0,
    }


def test_remote_response_repr_redacts_payload_and_headers():
    response = _common().RemoteHTTPResponse(
        200,
        {"access_token": "response-secret"},
        {"Set-Cookie": "session=response-secret"},
    )

    assert "response-secret" not in repr(response)
    assert "redacted" in repr(response).lower()


def test_remote_work_state_is_frozen_and_validates_typed_fields():
    from dataclasses import FrozenInstanceError
    from datetime import UTC, datetime

    from app.remote_discovery.contract import RemoteWorkState

    state = RemoteWorkState(
        source="pixiv",
        source_work_id="38362603",
        fetched_at=datetime.now(UTC),
        total_views=123456,
        total_bookmarks=7890,
        is_bookmarked=True,
    )

    with pytest.raises(FrozenInstanceError):
        state.total_views = 1
    with pytest.raises(ValueError, match="source is not supported"):
        RemoteWorkState("unknown", "1", datetime.now(UTC), 0, 0, False)
    with pytest.raises(ValueError, match="source_work_id must not be empty"):
        RemoteWorkState("pixiv", " ", datetime.now(UTC), 0, 0, False)
    with pytest.raises(ValueError, match="timezone-aware"):
        RemoteWorkState("pixiv", "1", datetime.now(), 0, 0, False)
    with pytest.raises(ValueError, match="total_views"):
        RemoteWorkState("pixiv", "1", datetime.now(UTC), True, 0, False)
    with pytest.raises(ValueError, match="total_bookmarks"):
        RemoteWorkState("pixiv", "1", datetime.now(UTC), 0, -1, False)
    with pytest.raises(ValueError, match="is_bookmarked"):
        RemoteWorkState("pixiv", "1", datetime.now(UTC), 0, 0, 1)


@pytest.mark.asyncio
async def test_remote_discovery_adapter_work_state_default_fails_closed():
    """Providers must opt in explicitly before a live work-state read is possible."""
    from app.remote_discovery.contract import RemoteDiscoveryAdapter

    class FailClosedAdapter(RemoteDiscoveryAdapter):
        source = "pixiv"
        auth_methods = ()

        async def list_collections(self, _credentials):
            raise AssertionError("not used")

        async def fetch_page(self, _credentials, **_kwargs):
            raise AssertionError("not used")

        def build_download_auth(self, _credentials):
            raise AssertionError("not used")

        async def validate_account(self, _credentials):
            raise AssertionError("not used")

    with pytest.raises(NotImplementedError, match="pixiv does not support remote work state"):
        await FailClosedAdapter().fetch_work_state({}, source_work_id="38362603")


@pytest.mark.asyncio
async def test_pixiv_work_state_uses_live_illust_detail_and_maps_volatile_fields():
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    transport = FixtureTransport(
        response(200, {"access_token": "short-lived-access", "expires_in": 3600}, {}),
        response(200, {"illust": {
            "id": 38362603,
            "total_view": 123456,
            "total_bookmarks": 7890,
            "is_bookmarked": True,
        }}, {}),
    )

    state = await PixivRemoteDiscoveryAdapter(transport).fetch_work_state(
        {"refresh_token": "refresh"}, source_work_id="38362603"
    )

    assert state.source == "pixiv"
    assert state.source_work_id == "38362603"
    assert state.total_views == 123456
    assert state.total_bookmarks == 7890
    assert state.is_bookmarked is True
    method, url, kwargs = transport.requests[1]
    assert method == "GET"
    assert url == "https://app-api.pixiv.net/v1/illust/detail"
    assert kwargs["params"] == {"illust_id": "38362603"}
    assert kwargs["headers"]["Authorization"] == "Bearer short-lived-access"
    assert kwargs["timeout"] == 10
    assert "timeout" not in transport.requests[0][2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("total_view", -1), ("total_view", True), ("total_bookmarks", "9"),
     ("is_bookmarked", 1)],
)
async def test_pixiv_work_state_rejects_malformed_volatile_fields(field, value):
    from app.remote_discovery.common import MalformedRemoteResponse
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter

    payload = {
        "id": 38362603,
        "total_view": 10,
        "total_bookmarks": 2,
        "is_bookmarked": False,
    }
    payload[field] = value
    response = _common().RemoteHTTPResponse
    adapter = PixivRemoteDiscoveryAdapter(FixtureTransport(
        response(200, {"access_token": "access"}, {}),
        response(200, {"illust": payload}, {}),
    ))

    with pytest.raises(MalformedRemoteResponse, match="Pixiv"):
        await adapter.fetch_work_state({"refresh_token": "refresh"}, source_work_id="38362603")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detail_response, expected_exception",
    [
        pytest.param((401, {}, {}), "RemoteReauthenticationRequired", id="unauthorized"),
        pytest.param((429, {}, {"Retry-After": "27"}), "RemoteRateLimited", id="rate-limited"),
    ],
)
async def test_pixiv_work_state_maps_detail_provider_errors(detail_response, expected_exception):
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    transport = FixtureTransport(
        response(200, {"access_token": "access"}, {}),
        response(*detail_response),
    )
    expected = getattr(_common(), expected_exception)

    with pytest.raises(expected):
        await PixivRemoteDiscoveryAdapter(transport).fetch_work_state(
            {"refresh_token": "refresh"}, source_work_id="38362603"
        )
    if expected_exception == "RemoteRateLimited":
        with pytest.raises(expected) as caught:
            await PixivRemoteDiscoveryAdapter(FixtureTransport(
                response(200, {"access_token": "access"}, {}),
                response(*detail_response),
            )).fetch_work_state({"refresh_token": "refresh"}, source_work_id="38362603")
        assert caught.value.retry_after_seconds == 27


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"illust": None},
        {"illust": {"id": 99, "total_view": 1, "total_bookmarks": 1, "is_bookmarked": False}},
    ],
    ids=["missing-illust", "null-illust", "wrong-id"],
)
async def test_pixiv_work_state_rejects_missing_or_mismatched_illust(payload):
    from app.remote_discovery.common import MalformedRemoteResponse
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    adapter = PixivRemoteDiscoveryAdapter(FixtureTransport(
        response(200, {"access_token": "access"}, {}),
        response(200, payload, {}),
    ))

    with pytest.raises(MalformedRemoteResponse, match="Pixiv"):
        await adapter.fetch_work_state({"refresh_token": "refresh"}, source_work_id="38362603")


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
    # The short timeout is exclusively for a live illust-detail read. Existing
    # authentication and following requests retain their prior timeout policy.
    assert "timeout" not in transport.requests[0][2]
    assert "timeout" not in transport.requests[1][2]


@pytest.mark.asyncio
async def test_pixiv_following_persists_three_normalized_recent_work_snapshots():
    """Dropping user_previews.illusts would remove the workbench's visual evidence."""
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    preview = {
        "user": {
            "id": 123,
            "name": "Pixiv Artist",
            "account": "pixiv_artist",
            "profile_image_urls": {"medium": "https://i.pximg.net/avatar.jpg"},
            "comment": "fixture profile",
            "is_followed": True,
        },
        "illusts": [_pixiv_illust(1000 + index) for index in range(4)],
        "novels": [],
        "is_muted": False,
    }
    adapter = PixivRemoteDiscoveryAdapter(FixtureTransport(
        response(200, {"access_token": "access"}, {}),
        response(200, {"user_previews": [preview], "next_url": None}, {}),
    ))

    page = await adapter.fetch_page(
        {"refresh_token": "refresh", "remote_user_id": "999"},
        page_size=20,
    )

    metadata = page.items[0].metadata
    assert metadata["has_illustration_preview"] is True
    assert [item["source_work_id"] for item in metadata["recent_works"]] == [
        "1000",
        "1001",
        "1002",
    ]
    assert metadata["recent_works"][0]["thumbnail_url"].endswith("1000_p0.jpg")
    assert metadata["recent_works"][0]["work_url"] == "https://www.pixiv.net/artworks/1000"


@pytest.mark.asyncio
async def test_pixiv_creator_detail_normalizes_profile_and_first_work_page_with_one_login():
    """The first drawer request must not refresh credentials once per upstream resource."""
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    user = {
        "id": 123,
        "name": "Pixiv Artist",
        "account": "pixiv_artist",
        "profile_image_urls": {"medium": "https://i.pximg.net/avatar.jpg"},
        "comment": "fixture profile",
        "is_followed": True,
    }
    transport = FixtureTransport(
        response(200, {"access_token": "access"}, {}),
        response(200, {
            "user": user,
            "profile": {
                "total_illusts": 12,
                "total_manga": 3,
                "total_novels": 1,
                "total_follow_users": 25,
                "total_mypixiv_users": 2,
                "total_illust_bookmarks_public": 40,
                "background_image_url": "https://i.pximg.net/user-profile/header.jpg",
                "webpage": "https://artist.example",
                "twitter_url": "https://twitter.com/pixiv_artist",
                "pawoo_url": "https://pawoo.net/@pixiv_artist",
                "gender": "female",
                "region": "Tokyo",
                "birth_day": "08-30",
                "birth_year": 2000,
                "job": "Illustrator",
            },
            "profile_publicity": {
                "gender": "public",
                "region": "private",
                "birth_day": "public",
                "birth_year": "private",
                "job": "public",
                "pawoo": True,
            },
            "workspace": {},
        }, {}),
        response(200, {
            "user": user,
            "illusts": [
                _pixiv_illust(456, work_type="manga", page_count=2, x_restrict=1),
                _pixiv_illust(457, work_type="ugoira"),
            ],
            "next_url": "https://app-api.pixiv.net/v1/user/illusts?user_id=123&offset=20",
        }, {}),
    )

    detail = await PixivRemoteDiscoveryAdapter(transport).fetch_creator_detail(
        {"refresh_token": "refresh"},
        source_creator_id="123",
        work_type="manga",
        page_size=20,
    )

    assert detail.profile.display_name == "Pixiv Artist"
    assert detail.profile.header_image_url == "https://i.pximg.net/user-profile/header.jpg"
    assert dict(detail.profile.work_counts) == {"illust": 12, "manga": 3, "novel": 1}
    assert dict(detail.profile.social_counts) == {
        "following": 25,
        "mypixiv": 2,
        "public_bookmarks": 40,
    }
    assert detail.profile.public_profile.gender == "female"
    assert detail.profile.public_profile.region is None
    assert detail.profile.public_profile.birth_day == "08-30"
    assert detail.profile.public_profile.birth_year is None
    assert detail.profile.public_profile.job == "Illustrator"
    assert [(link.kind, link.url) for link in detail.profile.links] == [
        ("website", "https://artist.example"),
        ("x", "https://twitter.com/pixiv_artist"),
        ("pawoo", "https://pawoo.net/@pixiv_artist"),
    ]
    assert detail.works.items[0].work_type == "manga"
    assert detail.works.items[0].page_count == 2
    assert detail.works.items[0].x_restrict == 1
    assert detail.works.items[0].preview_urls == (
        "https://i.pximg.net/img-master/456_p0.jpg",
        "https://i.pximg.net/img-master/456_p1.jpg",
    )
    assert detail.works.items[1].work_type == "ugoira"
    assert dict(detail.works.next_cursor) == {"offset": 20, "work_type": "manga"}
    assert [request[1] for request in transport.requests] == [
        "https://oauth.secure.pixiv.net/auth/token",
        "https://app-api.pixiv.net/v1/user/detail",
        "https://app-api.pixiv.net/v1/user/illusts",
    ]
    assert transport.requests[2][2]["params"] == {
        "user_id": "123",
        "offset": 0,
        "filter": "for_ios",
        "limit": 20,
        "type": "manga",
    }


@pytest.mark.asyncio
async def test_pixiv_default_profile_image_is_normalized_as_missing_optional_media():
    """Signing Pixiv's s.pximg.net placeholder must not make the whole profile fail."""
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    adapter = PixivRemoteDiscoveryAdapter(FixtureTransport(
        response(200, {"access_token": "access"}, {}),
        response(200, {
            "user": {
                "id": 123,
                "name": "No Avatar Artist",
                "account": "no_avatar",
                "profile_image_urls": {
                    "medium": "https://s.pximg.net/common/images/no_profile.png",
                },
                "comment": "",
                "is_followed": True,
            },
            "profile": {
                "total_illusts": 0,
                "total_manga": 0,
                "total_novels": 0,
            },
            "profile_publicity": {},
            "workspace": {},
        }, {}),
    ))

    profile = await adapter.fetch_creator_profile(
        {"refresh_token": "refresh"}, source_creator_id="123"
    )

    assert profile.avatar_url is None


@pytest.mark.asyncio
async def test_pixiv_creator_work_page_uses_validated_offset_cursor():
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    transport = FixtureTransport(
        response(200, {"access_token": "access"}, {}),
        response(200, {
            "user": {"id": 123, "name": "Artist", "account": "artist"},
            "illusts": [_pixiv_illust(789)],
            "next_url": None,
        }, {}),
    )

    page = await PixivRemoteDiscoveryAdapter(transport).fetch_creator_works(
        {"refresh_token": "refresh"},
        source_creator_id="123",
        work_type="manga",
        cursor={"offset": 20, "work_type": "manga"},
        page_size=20,
    )

    assert page.is_complete is True
    assert page.items[0].source_work_id == "789"
    assert transport.requests[1][2]["params"]["offset"] == 20
    assert transport.requests[1][2]["params"]["type"] == "manga"

    with pytest.raises(ValueError, match="work type"):
        await PixivRemoteDiscoveryAdapter(FixtureTransport()).fetch_creator_works(
            {"refresh_token": "refresh"},
            source_creator_id="123",
            work_type="illust",
            cursor={"offset": 20, "work_type": "manga"},
            page_size=20,
        )


@pytest.mark.asyncio
async def test_pixiv_creator_work_page_enforces_requested_size_when_upstream_ignores_limit():
    """Pixiv can return its fixed-size page even when a smaller limit is sent."""
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    transport = FixtureTransport(
        response(200, {"access_token": "access"}, {}),
        response(200, {
            "user": {"id": 123, "name": "Artist", "account": "artist"},
            "illusts": [_pixiv_illust(800 + index) for index in range(5)],
            "next_url": None,
        }, {}),
    )

    page = await PixivRemoteDiscoveryAdapter(transport).fetch_creator_works(
        {"refresh_token": "refresh"},
        source_creator_id="123",
        work_type="illust",
        cursor={"offset": 7, "work_type": "illust"},
        page_size=2,
    )

    assert [item.source_work_id for item in page.items] == ["800", "801"]
    assert dict(page.next_cursor) == {"offset": 9, "work_type": "illust"}
    assert page.is_complete is False


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
    adapter = XRemoteDiscoveryAdapter(
        transport,
        transaction_id_provider=FixtureTransactionIdProvider(),
    )

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
async def test_x_authenticated_web_requests_include_per_request_transaction_id():
    from app.remote_discovery.x import XRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    transport = FixtureTransport(
        response(
            200,
            {
                "id_str": "42",
                "name": "Owner",
                "screen_name": "owner",
                "description": "",
                "profile_image_url_https": "https://pbs.twimg.com/owner.jpg",
                "protected": False,
                "verified": False,
            },
            {},
        ),
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
        ),
    )
    transaction_ids = FixtureTransactionIdProvider()
    adapter = XRemoteDiscoveryAdapter(
        transport,
        transaction_id_provider=transaction_ids,
    )
    credentials = {
        "auth_method": "cookie",
        "cookie": "auth_token=secret; ct0=csrf",
        "remote_user_id": "42",
    }

    await adapter.validate_account(credentials)
    await adapter.fetch_page(credentials)

    assert transaction_ids.calls == [
        (
            "GET",
            "https://api.x.com/1.1/account/verify_credentials.json",
            "auth_token=secret; ct0=csrf",
        ),
        (
            "GET",
            "https://x.com/i/api/graphql/SaWqzw0TFAWMx1nXWjXoaQ/Following",
            "auth_token=secret; ct0=csrf",
        ),
    ]
    assert transport.requests[0][2]["headers"]["x-client-transaction-id"] == (
        "fixture-transaction-1"
    )
    assert transport.requests[1][2]["headers"]["x-client-transaction-id"] == (
        "fixture-transaction-2"
    )


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

    page = await XRemoteDiscoveryAdapter(
        transport,
        transaction_id_provider=FixtureTransactionIdProvider(),
    ).fetch_page(
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
async def test_x_oauth_401_refreshes_rotated_tokens_and_retries_exactly_once():
    """An OAuth access-token 401 is recoverable when refresh material exists."""

    from app.remote_discovery.x import XRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    transport = FixtureTransport(
        response(401, {"title": "expired"}, {}),
        response(
            200,
            {
                "access_token": "adapter-rotated-access-canary",
                "refresh_token": "adapter-rotated-refresh-canary",
            },
            {},
        ),
        response(200, {"data": {"id": "42"}}, {}),
        response(
            200,
            {
                "data": [{"id": "77", "name": "Recovered", "username": "recovered"}],
                "meta": {"result_count": 1},
            },
            {},
        ),
    )
    credentials = {
        "auth_method": "oauth2",
        "access_token": "adapter-expired-access-canary",
        "refresh_token": "adapter-original-refresh-canary",
        "client_id": "adapter-client-canary",
        "remote_user_id": "42",
    }

    page = await XRemoteDiscoveryAdapter(transport).fetch_page(credentials)

    assert page.items[0].source_creator_id == "77"
    assert [request[:2] for request in transport.requests] == [
        ("GET", "https://api.x.com/2/users/42/following"),
        ("POST", "https://api.x.com/2/oauth2/token"),
        ("GET", "https://api.x.com/2/users/me"),
        ("GET", "https://api.x.com/2/users/42/following"),
    ]
    assert transport.requests[3][2]["headers"]["Authorization"] == (
        "Bearer adapter-rotated-access-canary"
    )


@pytest.mark.asyncio
async def test_x_oauth_second_401_requires_reauthentication_without_refresh_loop():
    """The post-refresh retry is the only retry and still fails closed."""

    from app.remote_discovery.x import XRemoteDiscoveryAdapter

    response = _common().RemoteHTTPResponse
    transport = FixtureTransport(
        response(401, {"title": "expired"}, {}),
        response(
            200,
            {
                "access_token": "adapter-retry-access-canary",
                "refresh_token": "adapter-retry-refresh-canary",
            },
            {},
        ),
        response(200, {"data": {"id": "42"}}, {}),
        response(401, {"title": "still unauthorized"}, {}),
    )

    with pytest.raises(_common().RemoteReauthenticationRequired) as caught:
        await XRemoteDiscoveryAdapter(transport).fetch_page(
            {
                "auth_method": "oauth2",
                "access_token": "adapter-expired-access-canary",
                "refresh_token": "adapter-original-refresh-canary",
                "client_id": "adapter-client-canary",
                "remote_user_id": "42",
            }
        )

    assert caught.value.status_code == 401
    assert len(transport.requests) == 4


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
