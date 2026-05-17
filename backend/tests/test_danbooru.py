"""Tests for Danbooru service — URL classification and parsing."""

from app.services.danbooru import _classify_url, is_downloadable_url


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

    def test_iwara_downloadable(self):
        assert is_downloadable_url("https://www.iwara.tv/profile/username") is True
        assert is_downloadable_url("https://www.iwara.tv/video/abc") is False

    def test_non_downloadable(self):
        assert is_downloadable_url("https://twitter.com/artist") is False
        assert is_downloadable_url("https://youtube.com/@channel") is False
