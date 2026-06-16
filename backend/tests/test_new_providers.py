"""Tests for new providers: Danbooru, Pinterest, LOFTER."""

from app.providers.danbooru import DanbooruProvider
from app.providers.pinterest import PinterestProvider
from app.providers.lofter import LofterProvider


class TestDanbooruProvider:
    def setup_method(self):
        self.p = DanbooruProvider()

    def test_source_name(self):
        assert self.p.source_name == "danbooru"

    def test_capabilities(self):
        c = self.p.capabilities
        assert c.can_download is True
        assert c.supports_gallerydl is True
        assert c.supports_tags is True

    def test_validate_url_posts_tag_search(self):
        assert self.p.validate_url("https://danbooru.donmai.us/posts?tags=ask")

    def test_validate_url_artist(self):
        assert self.p.validate_url("https://danbooru.donmai.us/artists/12345")

    def test_validate_url_invalid(self):
        assert not self.p.validate_url("https://danbooru.donmai.us/posts/12345")
        assert not self.p.validate_url("https://pixiv.net/users/123")

    def test_normalize_url_posts(self):
        r = self.p.normalize_url("https://danbooru.donmai.us/posts?tags=ask")
        assert r == "https://danbooru.donmai.us/posts?tags=ask"

    def test_parse_source_creator(self):
        meta = {"tag_string_artist": "ask askzy", "id": 12345}
        r = self.p.parse_source_creator(meta)
        assert r["source_creator_id"] == "ask askzy"

    def test_parse_source_creator_no_tag(self):
        meta = {"tag_string_artist": "", "id": 12345}
        r = self.p.parse_source_creator(meta)
        assert r["source_creator_id"] == "unknown"

    def test_parse_work_source(self):
        meta = {"id": 12345, "tag_string_artist": "ask", "artist_commentary_desc": "Test"}
        r = self.p.parse_work_source(meta)
        assert r["source_work_id"] == "12345"
        assert r["source_creator_id"] == "ask"

    def test_parse_source_tags(self):
        meta = {
            "tag_string_artist": "ask",
            "tag_string_character": "hatsune_miku",
            "tag_string_general": "1girl",
            "tag_string_meta": "highres",
        }
        r = self.p.parse_source_tags(meta)
        cats = {t["category"] for t in r}
        assert "artist" in cats
        assert "character" in cats
        assert "general" in cats
        assert "meta" in cats

    def test_parse_source_tags_no_duplicates(self):
        meta = {"tag_string_artist": "ask ask", "tag_string_general": "ask 1girl"}
        r = self.p.parse_source_tags(meta)
        names = [t["original_name"] for t in r]
        assert names.count("ask") == 1


class TestPinterestProvider:
    def setup_method(self):
        self.p = PinterestProvider()

    def test_source_name(self):
        assert self.p.source_name == "pinterest"

    def test_capabilities(self):
        c = self.p.capabilities
        assert c.can_download is True
        assert c.supports_tags is False

    def test_validate_url_pin(self):
        assert self.p.validate_url("https://www.pinterest.com/pin/12345/")

    def test_validate_url_user(self):
        assert self.p.validate_url("https://www.pinterest.com/username/pins/")

    def test_validate_url_invalid(self):
        assert not self.p.validate_url("https://www.pinterest.com/")

    def test_normalize_url_pin(self):
        r = self.p.normalize_url("pinterest.com/pin/12345")
        assert r == "https://www.pinterest.com/pin/12345"

    def test_parse_work_source(self):
        meta = {"id": 12345, "description": "A test pin"}
        r = self.p.parse_work_source(meta)
        assert r["source_work_id"] == "12345"

    def test_parse_source_tags_returns_empty(self):
        assert self.p.parse_source_tags({}) == []


class TestLofterProvider:
    def setup_method(self):
        self.p = LofterProvider()

    def test_source_name(self):
        assert self.p.source_name == "lofter"

    def test_capabilities(self):
        c = self.p.capabilities
        assert c.can_download is True

    def test_validate_url_blog(self):
        assert self.p.validate_url("https://agemoagm14574.lofter.com/")

    def test_validate_url_post(self):
        assert self.p.validate_url("https://agemoagm14574.lofter.com/post/312dae80_1ccaa179b")

    def test_validate_url_www_rejected(self):
        assert not self.p.validate_url("https://www.lofter.com/")
        assert not self.p.validate_url("https://www.lofter.com/mentionredirect.do?blogId=838116774")

    def test_normalize_url_post(self):
        r = self.p.normalize_url("agemoagm14574.lofter.com/post/312dae80_1ccaa179b")
        assert r == "https://agemoagm14574.lofter.com/post/312dae80_1ccaa179b"

    def test_parse_source_creator(self):
        meta = {"blog_name": "agemoagm14574"}
        r = self.p.parse_source_creator(meta)
        assert r["source_creator_id"] == "agemoagm14574"

    def test_parse_work_source(self):
        meta = {"id": "312dae80_1ccaa179b", "blog_name": "agemoagm14574", "title": "Test"}
        r = self.p.parse_work_source(meta)
        assert r["source_work_id"] == "312dae80_1ccaa179b"

    def test_build_gallerydl_config_includes_id(self):
        # Directory is controlled by the admin gallery-dl config, not provider defaults.
        cfg = self.p.build_gallerydl_config(None)
        assert "directory" not in cfg["extractor"]["lofter"]
