"""Tests for Danbooru service — URL classification, parsing, error classification."""

import urllib.error

import pytest

import app.services.danbooru as danbooru_svc
from app.services.danbooru import DanbooruUnavailableError, _classify_url, is_downloadable_url


class TestURLClassification:
    def test_pixiv_urls(self):
        assert _classify_url("https://www.pixiv.net/en/users/1980643") == "pixiv"
        assert _classify_url("https://www.pixiv.net/artworks/38362603") == "pixiv"

    def test_pixiv_subtype_urls(self):
        assert _classify_url("https://sketch.pixiv.net/@user") == "pixiv_sketch"
        assert _classify_url("https://pixiv.net/stacc/something") == "pixiv_stacc"
        assert _classify_url("https://www.pixiv.net/fanbox/creator/123") == "fanbox"
        assert _classify_url("https://fanbox.cc/@user") == "fanbox"

    def test_twitter_x_urls(self):
        assert _classify_url("https://twitter.com/artist") == "x"
        assert _classify_url("https://x.com/artist") == "x"

    def test_iwara_urls(self):
        assert _classify_url("https://www.iwara.tv/video/abc123") == "iwara"
        assert _classify_url("https://www.iwara.tv/profile/user") == "iwara"

    def test_video_platforms(self):
        assert _classify_url("https://www.youtube.com/watch?v=abc") == "youtube"
        assert _classify_url("https://youtu.be/abc") == "youtube"
        assert _classify_url("https://www.bilibili.com/video/av123") == "bilibili"
        assert _classify_url("https://www.nicovideo.jp/watch/sm123") == "nicovideo"

    def test_art_platforms(self):
        assert _classify_url("https://danbooru.donmai.us/posts/123") == "danbooru"
        assert _classify_url("https://www.deviantart.com/user/art/title") == "deviantart"
        assert _classify_url("https://www.artstation.com/artwork/abc") == "artstation"

    def test_social_media(self):
        assert _classify_url("https://www.instagram.com/user/") == "instagram"
        assert _classify_url("https://user.tumblr.com/post/123") == "tumblr"
        assert _classify_url("https://www.tiktok.com/@user/video/123") == "tiktok"
        assert _classify_url("https://bsky.app/profile/user.bsky.social") == "bluesky"

    def test_funding_platforms(self):
        assert _classify_url("https://skeb.jp/@user") == "skeb"
        assert _classify_url("https://www.patreon.com/user") == "patreon"
        assert _classify_url("https://fantia.jp/posts/123") == "fantia"

    def test_chinese_platforms(self):
        assert _classify_url("https://www.weibo.com/u/123") == "weibo"
        assert _classify_url("https://www.xiaohongshu.com/user/123") == "xiaohongshu"
        assert _classify_url("https://username.lofter.com") == "lofter"
        assert _classify_url("https://username.lofter.com/post/abc123") == "lofter"
        # www.lofter.com is the portal, not a user blog
        assert _classify_url("https://www.lofter.com") == "website"

    def test_x_no_false_positive(self):
        # ax.com should NOT be classified as x
        assert _classify_url("https://ax.com/user") != "x"
        # but real x/twitter URLs should
        assert _classify_url("https://x.com/artist") == "x"
        assert _classify_url("https://twitter.com/artist") == "x"

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://pixiv.net.evil.example/users/1", "website"),
            ("https://evilpixiv.net/users/1", "website"),
            ("https://pixiv.net@evil.example/users/1", "website"),
            ("https://x.com.evil.example/artist", "website"),
            ("https://evil.example/path/twitter.com/artist", "website"),
            ("ftp://pixiv.net/users/1", "website"),
            ("mailto:user@pixiv.net", "website"),
            ("https://cdn.sketch.pixiv.net/@artist", "pixiv_sketch"),
            ("https://PIXIV.NET./users/1", "pixiv"),
        ],
    )
    def test_domain_boundaries(self, url, expected):
        assert _classify_url(url) == expected

    def test_scheme_less_url(self):
        assert _classify_url("www.pixiv.net/users/1980643") == "pixiv"
        assert _classify_url("//www.pixiv.net/users/1980643") == "pixiv"

    def test_website_fallback(self):
        assert _classify_url("https://example.com/profile") == "website"
        assert _classify_url("https://random-site.org/about") == "website"

    def test_linktree_carrd(self):
        assert _classify_url("https://linktr.ee/artist") == "linktree"
        assert _classify_url("https://artist.carrd.co") == "carrd"


class TestDownloadableURL:
    def test_pixiv_downloadable(self):
        assert is_downloadable_url("https://www.pixiv.net/en/users/1980643") is True
        assert is_downloadable_url("https://www.pixiv.net/artworks/123") is False  # artwork URL, not user

    def test_x_downloadable(self):
        assert is_downloadable_url("https://twitter.com/artist") is True
        assert is_downloadable_url("https://x.com/artist") is True

    def test_weibo_downloadable(self):
        assert is_downloadable_url("https://weibo.com/u/1234567890") is True
        assert is_downloadable_url("https://weibo.com/username") is True

    def test_iwara_downloadable(self):
        assert is_downloadable_url("https://www.iwara.tv/profile/username") is True
        assert is_downloadable_url("https://www.iwara.tv/users/username") is True
        assert is_downloadable_url("https://www.iwara.tv/video/abc") is False

    def test_lofter_downloadable(self):
        assert is_downloadable_url("https://username.lofter.com") is True
        assert is_downloadable_url("https://www.lofter.com") is False

    def test_non_downloadable(self):
        assert is_downloadable_url("https://youtube.com/@channel") is False
        assert is_downloadable_url("https://danbooru.donmai.us/posts/123") is False  # individual post, not tag search


class _RaisingOpener:
    def __init__(self, exc_factory):
        self.exc_factory = exc_factory
        self.calls = 0

    def open(self, req, timeout=None):
        self.calls += 1
        raise self.exc_factory(req)


class TestErrorClassification:
    """Network failures must raise DanbooruUnavailableError, never masquerade as 'not found'."""

    def test_get_raises_unavailable_after_retries(self, monkeypatch):
        opener = _RaisingOpener(lambda req: ConnectionResetError("Remote end closed connection"))
        monkeypatch.setattr(danbooru_svc, "_opener", opener)
        monkeypatch.setattr(danbooru_svc.time, "sleep", lambda s: None)
        with pytest.raises(DanbooruUnavailableError):
            danbooru_svc._get("/artists.json?limit=1")
        assert opener.calls == danbooru_svc.MAX_RETRIES

    def test_get_returns_none_on_404_without_retry(self, monkeypatch):
        opener = _RaisingOpener(
            lambda req: urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        )
        monkeypatch.setattr(danbooru_svc, "_opener", opener)
        monkeypatch.setattr(danbooru_svc.time, "sleep", lambda s: None)
        assert danbooru_svc._get("/artists/999999999.json") is None
        assert opener.calls == 1

    def test_search_propagates_unavailable(self, monkeypatch):
        def _raise(path):
            raise DanbooruUnavailableError("net down")

        monkeypatch.setattr(danbooru_svc, "_get", _raise)
        with pytest.raises(DanbooruUnavailableError):
            danbooru_svc.search_by_url("https://www.pixiv.net/users/75220791")
        with pytest.raises(DanbooruUnavailableError):
            danbooru_svc.search_by_name("yosei_bin")

    def test_search_and_extract_falls_back_when_detail_unavailable(self, monkeypatch):
        best = {"id": 240355, "name": "yosei_bin"}
        monkeypatch.setattr(danbooru_svc, "search_by_url", lambda url: [best])

        def _raise(artist_id):
            raise DanbooruUnavailableError("detail fetch failed")

        monkeypatch.setattr(danbooru_svc, "get_artist", _raise)
        artist, links = danbooru_svc.search_and_extract(source_url="https://www.pixiv.net/users/75220791")
        assert artist is best
        assert links and links[0]["link_type"] == "danbooru"

    async def test_disk_import_enrichment_reports_error_not_not_found(self, monkeypatch):
        from app.services import disk_identity as di

        def _raise(**kwargs):
            raise DanbooruUnavailableError("net down")

        monkeypatch.setattr(di.danbooru_svc, "search_and_extract", _raise)
        identity = di.MetadataIdentity(
            source="pixiv",
            source_creator_id="75220791",
            source_url="https://www.pixiv.net/users/75220791",
            display_name="yosei",
            raw_metadata={},
            creator_dir="yosei",
        )
        artist, links, status = await di._try_danbooru_enrichment(identity)
        assert artist is None and links == []
        assert status == "danbooru_error:DanbooruUnavailableError"
