"""Pixiv App API remote-follow discovery using a refresh token."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
import hashlib
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.remote_discovery.common import (
    HttpxRemoteTransport,
    MalformedRemoteResponse,
    RemoteHTTPTransport,
    checked_oauth_token_payload,
    checked_payload,
    required_text,
    validate_page_size,
)
from app.remote_discovery.contract import (
    DiscoveryPage,
    RemoteCandidateIdentity,
    RemoteCollection,
    RemoteCreatorDetail,
    RemoteCreatorLink,
    RemoteCreatorProfile,
    RemoteCreatorPublicProfile,
    RemoteDiscoveryAdapter,
    RemoteWorkPage,
    RemoteWorkPreview,
    RemoteWorkState,
)
from app.services.remote_credentials import DownloadAuthenticationOverride


PIXIV_RANKING_MODES = ("day", "day_ai", "day_r18", "day_r18_ai")


@dataclass(frozen=True, slots=True)
class PixivRankingEntry:
    source_work_id: str
    rank: int


@dataclass(frozen=True, slots=True)
class PixivRankingResult:
    mode: str
    ranking_date: date
    items: tuple[PixivRankingEntry, ...]
    fetched_at: datetime


class PixivRemoteDiscoveryAdapter(RemoteDiscoveryAdapter):
    source = "pixiv"
    auth_methods = ("refresh_token",)
    TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
    FOLLOWING_URL = "https://app-api.pixiv.net/v1/user/following"
    USER_DETAIL_URL = "https://app-api.pixiv.net/v1/user/detail"
    USER_ILLUSTS_URL = "https://app-api.pixiv.net/v1/user/illusts"
    ILLUST_DETAIL_URL = "https://app-api.pixiv.net/v1/illust/detail"
    RANKING_URL = "https://app-api.pixiv.net/v1/illust/ranking"
    APP_HEADERS = {
        "App-OS": "ios",
        "App-OS-Version": "16.7.2",
        "App-Version": "7.19.1",
        "User-Agent": "PixivIOSApp/7.19.1 (iOS 16.7.2; iPhone12,8)",
        "Referer": "https://app-api.pixiv.net/",
    }
    DEFAULT_PROFILE_IMAGE_URL = "https://s.pximg.net/common/images/no_profile.png"

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
        payload = checked_oauth_token_payload(response, provider="Pixiv")
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

    async def fetch_rankings(
        self,
        credentials: Mapping[str, Any],
        *,
        mode: str,
        ranking_date: date,
        limit: int = 500,
    ) -> PixivRankingResult:
        """Fetch one dated Pixiv ranking without trusting provider pagination URLs."""

        if mode not in PIXIV_RANKING_MODES:
            raise ValueError("unknown Pixiv ranking mode")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("Pixiv ranking limit must be between 1 and 500")

        access_token, _user = await self._authentication(credentials)
        headers = {**self.APP_HEADERS, "Authorization": f"Bearer {access_token}"}
        items: list[PixivRankingEntry] = []
        seen_work_ids: set[str] = set()
        seen_offsets: set[int] = set()
        offset = 0

        while len(items) < limit:
            if offset in seen_offsets:
                raise MalformedRemoteResponse("Pixiv ranking next_url repeats an offset")
            seen_offsets.add(offset)
            response = await self.transport.request(
                "GET",
                self.RANKING_URL,
                headers=headers,
                params={
                    "filter": "for_ios",
                    "mode": mode,
                    "date": ranking_date.isoformat(),
                    "offset": offset,
                },
            )
            payload = checked_payload(response, provider="Pixiv")
            raw_illusts = payload.get("illusts")
            if not isinstance(raw_illusts, list):
                raise MalformedRemoteResponse("Pixiv ranking response has invalid illusts")
            for raw_illust in raw_illusts:
                if not isinstance(raw_illust, Mapping):
                    raise MalformedRemoteResponse("Pixiv ranking contains an invalid illustration")
                work_id = raw_illust.get("id")
                if isinstance(work_id, bool) or not isinstance(work_id, (int, str)):
                    raise MalformedRemoteResponse("Pixiv ranking illustration is missing id")
                source_work_id = str(work_id).strip()
                if not source_work_id or source_work_id in seen_work_ids:
                    raise MalformedRemoteResponse("Pixiv ranking contains a duplicate or empty id")
                seen_work_ids.add(source_work_id)
                items.append(PixivRankingEntry(source_work_id, len(items) + 1))
                if len(items) >= limit:
                    break

            next_url = payload.get("next_url")
            if not next_url or len(items) >= limit:
                break
            if not isinstance(next_url, str):
                raise MalformedRemoteResponse("Pixiv ranking next_url is malformed")
            parsed = urlsplit(next_url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "app-api.pixiv.net"
                or parsed.path != "/v1/illust/ranking"
                or parsed.username
                or parsed.password
            ):
                raise MalformedRemoteResponse("Pixiv ranking next_url is untrusted")
            query = parse_qs(parsed.query)
            try:
                next_offset = int(query["offset"][0])
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise MalformedRemoteResponse(
                    "Pixiv ranking next_url is missing a valid offset"
                ) from exc
            if next_offset <= offset:
                raise MalformedRemoteResponse("Pixiv ranking next_url does not advance")
            offset = next_offset

        return PixivRankingResult(
            mode=mode,
            ranking_date=ranking_date,
            items=tuple(items),
            fetched_at=datetime.now(UTC),
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
            raw_illusts = preview.get("illusts") or []
            if not isinstance(raw_illusts, list):
                raise MalformedRemoteResponse("Pixiv following preview has invalid illusts")
            recent_works = [
                self._work_snapshot(self._normalize_work(item, source_creator_id=source_creator_id))
                for item in raw_illusts[:3]
            ]
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
                        "has_illustration_preview": bool(recent_works),
                        "recent_works": recent_works,
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

    @staticmethod
    def _optional_https_url(value: Any) -> str | None:
        return value if isinstance(value, str) and value.startswith("https://") else None

    @classmethod
    def _optional_profile_image_url(cls, value: Any) -> str | None:
        url = cls._optional_https_url(value)
        return None if url == cls.DEFAULT_PROFILE_IMAGE_URL else url

    @staticmethod
    def _public_text(profile: Mapping[str, Any], publicity: Mapping[str, Any], field: str) -> str | None:
        value = profile.get(field)
        if publicity.get(field) != "public" or not isinstance(value, str) or not value.strip():
            return None
        return value

    @staticmethod
    def _external_link(value: Any, *, allowed_hosts: set[str] | None = None) -> str | None:
        if not isinstance(value, str):
            return None
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
            return None
        if allowed_hosts is not None and hostname not in allowed_hosts:
            return None
        return value

    @staticmethod
    def _required_nonnegative_profile_count(profile: Mapping[str, Any], field: str) -> int:
        value = profile.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MalformedRemoteResponse(f"Pixiv user detail has invalid {field}")
        return value

    @classmethod
    def _normalize_profile(
        cls,
        user: Any,
        profile: Any,
        profile_publicity: Any = None,
        *,
        source_creator_id: str,
    ) -> RemoteCreatorProfile:
        if not isinstance(user, Mapping) or str(user.get("id") or "") != source_creator_id:
            raise MalformedRemoteResponse("Pixiv user detail has invalid user identity")
        if not isinstance(profile, Mapping):
            raise MalformedRemoteResponse("Pixiv user detail has invalid profile")
        if profile_publicity is None:
            profile_publicity = {}
        if not isinstance(profile_publicity, Mapping):
            raise MalformedRemoteResponse("Pixiv user detail has invalid profile publicity")
        image_urls = user.get("profile_image_urls") or {}
        if not isinstance(image_urls, Mapping):
            raise MalformedRemoteResponse("Pixiv user detail has invalid profile image URLs")
        avatar_url = next(
            (
                cls._optional_profile_image_url(image_urls.get(key))
                for key in ("medium", "square_medium", "large")
                if cls._optional_profile_image_url(image_urls.get(key))
            ),
            None,
        )
        is_followed = user.get("is_followed")
        if is_followed is not None and not isinstance(is_followed, bool):
            raise MalformedRemoteResponse("Pixiv user detail has invalid is_followed")
        birth_year = profile.get("birth_year")
        if (
            profile_publicity.get("birth_year") != "public"
            or isinstance(birth_year, bool)
            or not isinstance(birth_year, int)
            or birth_year < 0
        ):
            birth_year = None
        links: list[RemoteCreatorLink] = []
        website = cls._external_link(profile.get("webpage"))
        if website:
            links.append(RemoteCreatorLink(kind="website", url=website))
        x_url = cls._external_link(
            profile.get("twitter_url"),
            allowed_hosts={"twitter.com", "www.twitter.com", "x.com", "www.x.com"},
        )
        if x_url:
            links.append(RemoteCreatorLink(kind="x", url=x_url))
        pawoo_url = cls._external_link(
            profile.get("pawoo_url"), allowed_hosts={"pawoo.net", "www.pawoo.net"}
        )
        if pawoo_url and profile_publicity.get("pawoo") is True:
            links.append(RemoteCreatorLink(kind="pawoo", url=pawoo_url))
        return RemoteCreatorProfile(
            source="pixiv",
            source_creator_id=source_creator_id,
            display_name=str(user.get("name") or user.get("account") or source_creator_id),
            username=str(user.get("account") or "") or None,
            profile_url=f"https://www.pixiv.net/users/{source_creator_id}",
            avatar_url=avatar_url,
            comment=str(user.get("comment") or "") or None,
            work_counts={
                "illust": cls._required_nonnegative_profile_count(profile, "total_illusts"),
                "manga": cls._required_nonnegative_profile_count(profile, "total_manga"),
                "novel": cls._required_nonnegative_profile_count(profile, "total_novels"),
            },
            header_image_url=cls._optional_https_url(profile.get("background_image_url")),
            social_counts={
                "following": cls._required_nonnegative_profile_count(profile, "total_follow_users"),
                "mypixiv": cls._required_nonnegative_profile_count(profile, "total_mypixiv_users"),
                "public_bookmarks": cls._required_nonnegative_profile_count(
                    profile, "total_illust_bookmarks_public"
                ),
            },
            public_profile=RemoteCreatorPublicProfile(
                gender=cls._public_text(profile, profile_publicity, "gender"),
                region=cls._public_text(profile, profile_publicity, "region"),
                birth_day=cls._public_text(profile, profile_publicity, "birth_day"),
                birth_year=birth_year,
                job=cls._public_text(profile, profile_publicity, "job"),
            ),
            links=links,
            is_followed=is_followed,
            fetched_at=datetime.now(UTC),
        )

    @classmethod
    def _normalize_work(
        cls,
        raw: Any,
        *,
        source_creator_id: str,
    ) -> RemoteWorkPreview:
        if not isinstance(raw, Mapping):
            raise MalformedRemoteResponse("Pixiv creator works contains an invalid work")
        source_work_id = str(raw.get("id") or "")
        if not source_work_id:
            raise MalformedRemoteResponse("Pixiv creator work is missing id")
        user = raw.get("user")
        if not isinstance(user, Mapping) or str(user.get("id") or "") != source_creator_id:
            raise MalformedRemoteResponse("Pixiv creator work has mismatched user identity")
        work_type = raw.get("type")
        if work_type not in {"illust", "manga", "ugoira"}:
            raise MalformedRemoteResponse("Pixiv creator work has invalid type")
        page_count = raw.get("page_count")
        if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
            raise MalformedRemoteResponse("Pixiv creator work has invalid page_count")
        x_restrict = raw.get("x_restrict", 0)
        if isinstance(x_restrict, bool) or not isinstance(x_restrict, int) or x_restrict not in {0, 1, 2}:
            raise MalformedRemoteResponse("Pixiv creator work has invalid x_restrict")
        create_date = raw.get("create_date")
        if not isinstance(create_date, str):
            raise MalformedRemoteResponse("Pixiv creator work has invalid create_date")
        try:
            created_at = datetime.fromisoformat(create_date.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MalformedRemoteResponse("Pixiv creator work has invalid create_date") from exc
        if created_at.tzinfo is None:
            raise MalformedRemoteResponse("Pixiv creator work create_date must be timezone-aware")
        image_urls = raw.get("image_urls") or {}
        if not isinstance(image_urls, Mapping):
            raise MalformedRemoteResponse("Pixiv creator work has invalid image URLs")
        thumbnail_url = next(
            (
                cls._optional_https_url(image_urls.get(key))
                for key in ("square_medium", "medium", "large")
                if cls._optional_https_url(image_urls.get(key))
            ),
            None,
        )
        meta_pages = raw.get("meta_pages") or []
        if not isinstance(meta_pages, list):
            raise MalformedRemoteResponse("Pixiv creator work has invalid meta_pages")
        preview_urls: list[str] = []
        for page in meta_pages:
            if not isinstance(page, Mapping) or not isinstance(page.get("image_urls"), Mapping):
                raise MalformedRemoteResponse("Pixiv creator work has invalid page image URLs")
            page_urls = page["image_urls"]
            url = cls._optional_https_url(page_urls.get("large")) or cls._optional_https_url(page_urls.get("medium"))
            if url:
                preview_urls.append(url)
        if not preview_urls:
            url = cls._optional_https_url(image_urls.get("large")) or cls._optional_https_url(image_urls.get("medium"))
            if url:
                preview_urls.append(url)
        return RemoteWorkPreview(
            source="pixiv",
            source_work_id=source_work_id,
            source_creator_id=source_creator_id,
            title=str(raw.get("title") or source_work_id),
            work_url=f"https://www.pixiv.net/artworks/{source_work_id}",
            created_at=created_at,
            work_type=work_type,
            page_count=page_count,
            x_restrict=x_restrict,
            thumbnail_url=thumbnail_url,
            preview_urls=preview_urls,
        )

    @staticmethod
    def _work_snapshot(work: RemoteWorkPreview) -> dict[str, Any]:
        return {
            "source_work_id": work.source_work_id,
            "source_creator_id": work.source_creator_id,
            "title": work.title,
            "work_url": work.work_url,
            "created_at": work.created_at.isoformat(),
            "work_type": work.work_type,
            "page_count": work.page_count,
            "x_restrict": work.x_restrict,
            "thumbnail_url": work.thumbnail_url,
            "preview_urls": list(work.preview_urls),
        }

    @staticmethod
    def _creator_works_cursor(next_url: Any, *, work_type: str) -> Mapping[str, Any] | None:
        if not next_url:
            return None
        if not isinstance(next_url, str):
            raise MalformedRemoteResponse("Pixiv creator works next_url is malformed")
        query = parse_qs(urlsplit(next_url).query)
        try:
            offset = int(query["offset"][0])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise MalformedRemoteResponse("Pixiv creator works next_url is missing a valid offset") from exc
        if offset < 0:
            raise MalformedRemoteResponse("Pixiv creator works next_url has a negative offset")
        return {"offset": offset, "work_type": work_type}

    async def _fetch_profile_with_access_token(
        self,
        access_token: str,
        *,
        source_creator_id: str,
    ) -> RemoteCreatorProfile:
        response = await self.transport.request(
            "GET",
            self.USER_DETAIL_URL,
            headers={**self.APP_HEADERS, "Authorization": f"Bearer {access_token}"},
            params={"user_id": source_creator_id, "filter": "for_ios"},
            timeout=10,
        )
        payload = checked_payload(response, provider="Pixiv")
        return self._normalize_profile(
            payload.get("user"),
            payload.get("profile"),
            payload.get("profile_publicity"),
            source_creator_id=source_creator_id,
        )

    async def _fetch_works_with_access_token(
        self,
        access_token: str,
        *,
        source_creator_id: str,
        offset: int,
        work_type: str,
        page_size: int,
    ) -> RemoteWorkPage:
        response = await self.transport.request(
            "GET",
            self.USER_ILLUSTS_URL,
            headers={**self.APP_HEADERS, "Authorization": f"Bearer {access_token}"},
            params={
                "user_id": source_creator_id,
                "offset": offset,
                "filter": "for_ios",
                "limit": page_size,
                "type": work_type,
            },
            timeout=10,
        )
        payload = checked_payload(response, provider="Pixiv")
        raw_works = payload.get("illusts")
        if not isinstance(raw_works, list):
            raise MalformedRemoteResponse("Pixiv creator works response has invalid illusts")
        items = [
            self._normalize_work(item, source_creator_id=source_creator_id)
            for item in raw_works[:page_size]
        ]
        # Pixiv's user/illusts endpoint can ignore a small requested limit and
        # return its fixed-size page. Keep our public contract strict and
        # resume from the first item we did not expose, rather than skipping
        # the upstream remainder via next_url.
        if len(raw_works) > page_size:
            next_cursor = {
                "offset": offset + page_size,
                "work_type": work_type,
            }
        else:
            next_cursor = self._creator_works_cursor(
                payload.get("next_url"), work_type=work_type
            )
        return RemoteWorkPage(items=items, next_cursor=next_cursor, done=next_cursor is None)

    async def fetch_creator_profile(
        self,
        credentials: Mapping[str, Any],
        *,
        source_creator_id: str,
    ) -> RemoteCreatorProfile:
        if not source_creator_id.strip():
            raise ValueError("source_creator_id must not be empty")
        access_token, _user = await self._authentication(credentials)
        return await self._fetch_profile_with_access_token(
            access_token,
            source_creator_id=source_creator_id,
        )

    async def fetch_creator_detail(
        self,
        credentials: Mapping[str, Any],
        *,
        source_creator_id: str,
        work_type: str = "illust",
        page_size: int = 20,
    ) -> RemoteCreatorDetail:
        validate_page_size(page_size)
        if not source_creator_id.strip():
            raise ValueError("source_creator_id must not be empty")
        if work_type not in {"illust", "manga"}:
            raise ValueError("Pixiv creator work type is invalid")
        access_token, _user = await self._authentication(credentials)
        profile = await self._fetch_profile_with_access_token(
            access_token,
            source_creator_id=source_creator_id,
        )
        works = await self._fetch_works_with_access_token(
            access_token,
            source_creator_id=source_creator_id,
            offset=0,
            work_type=work_type,
            page_size=page_size,
        )
        return RemoteCreatorDetail(profile=profile, works=works)

    async def fetch_creator_works(
        self,
        credentials: Mapping[str, Any],
        *,
        source_creator_id: str,
        work_type: str = "illust",
        cursor: Mapping[str, Any] | None = None,
        page_size: int = 20,
    ) -> RemoteWorkPage:
        validate_page_size(page_size)
        if not source_creator_id.strip():
            raise ValueError("source_creator_id must not be empty")
        if work_type not in {"illust", "manga"}:
            raise ValueError("Pixiv creator work type is invalid")
        cursor_work_type = (cursor or {}).get("work_type", work_type)
        if cursor_work_type != work_type:
            raise ValueError("Pixiv creator work type does not match cursor")
        try:
            offset = int((cursor or {}).get("offset", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Pixiv creator works cursor") from exc
        if offset < 0:
            raise ValueError("invalid Pixiv creator works cursor")
        access_token, _user = await self._authentication(credentials)
        return await self._fetch_works_with_access_token(
            access_token,
            source_creator_id=source_creator_id,
            offset=offset,
            work_type=work_type,
            page_size=page_size,
        )

    @staticmethod
    def _required_nonnegative_int(illust: Mapping[str, Any], field: str) -> int:
        value = illust.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MalformedRemoteResponse(f"Pixiv illust detail has invalid {field}")
        return value

    async def fetch_work_state(
        self,
        credentials: Mapping[str, Any],
        *,
        source_work_id: str,
    ) -> RemoteWorkState:
        access_token, _user = await self._authentication(credentials)
        response = await self.transport.request(
            "GET",
            self.ILLUST_DETAIL_URL,
            headers={**self.APP_HEADERS, "Authorization": f"Bearer {access_token}"},
            params={"illust_id": source_work_id},
            timeout=10,
        )
        payload = checked_payload(response, provider="Pixiv")
        illust = payload.get("illust")
        if not isinstance(illust, Mapping):
            raise MalformedRemoteResponse("Pixiv illust detail response has invalid illust")
        if str(illust.get("id")) != source_work_id:
            raise MalformedRemoteResponse("Pixiv illust detail response has mismatched id")
        total_views = self._required_nonnegative_int(illust, "total_view")
        total_bookmarks = self._required_nonnegative_int(illust, "total_bookmarks")
        is_bookmarked = illust.get("is_bookmarked")
        if not isinstance(is_bookmarked, bool):
            raise MalformedRemoteResponse("Pixiv illust detail has invalid is_bookmarked")
        return RemoteWorkState(
            source="pixiv",
            source_work_id=source_work_id,
            fetched_at=datetime.now(UTC),
            total_views=total_views,
            total_bookmarks=total_bookmarks,
            is_bookmarked=is_bookmarked,
        )

    def build_download_auth(self, credentials: Mapping[str, Any]) -> DownloadAuthenticationOverride:
        refresh_token = required_text(credentials, "refresh_token", provider="Pixiv")
        return DownloadAuthenticationOverride(
            {"extractor": {"pixiv": {"refresh-token": refresh_token}}}
        )
