"""Tests for _should_auto_link_creator — the auto-link guard in import_runner."""

import pytest
from dataclasses import dataclass

from app.jobs.import_runner import _should_auto_link_creator
from app.providers import registry


@dataclass
class FakeSubscriptionSource:
    source: str
    source_url: str


# ── X / Twitter ──────────────────────────────────────────────────

class TestAutoLinkX:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.provider = registry.get("x")

    def test_match_own_tweet(self):
        """Own tweet: URL creator == metadata creator → should auto-link."""
        ss = FakeSubscriptionSource(source="x", source_url="https://x.com/artist_handle")
        meta = {
            "id_str": "123",
            "user": {"id": 111, "name": "artist_handle", "nick": "Artist"},
        }
        should_link, url_c, meta_c = _should_auto_link_creator(self.provider, meta, ss)
        assert should_link is True
        assert url_c == "artist_handle"
        assert meta_c == "artist_handle"

    def test_mismatch_retweet(self):
        """Retweet: metadata has retweeted user → should NOT auto-link."""
        ss = FakeSubscriptionSource(source="x", source_url="https://x.com/artist_handle")
        meta = {
            "id_str": "456",
            "user": {"id": 222, "name": "other_artist", "nick": "Other"},
        }
        should_link, url_c, meta_c = _should_auto_link_creator(self.provider, meta, ss)
        assert should_link is False
        assert url_c == "artist_handle"
        assert meta_c == "other_artist"


# ── Pixiv ────────────────────────────────────────────────────────

class TestAutoLinkPixiv:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.provider = registry.get("pixiv")

    def test_match_own_artwork(self):
        """Subscribed user's own artwork → should auto-link."""
        ss = FakeSubscriptionSource(source="pixiv", source_url="https://www.pixiv.net/users/1980643")
        meta = {"user": {"id": 1980643, "name": "My Artist", "account": "my_artist"}}
        should_link, url_c, meta_c = _should_auto_link_creator(self.provider, meta, ss)
        assert should_link is True
        assert url_c == "1980643"
        assert meta_c == "1980643"

    def test_mismatch_bookmark(self):
        """Bookmarked artwork from another artist → should NOT auto-link."""
        ss = FakeSubscriptionSource(source="pixiv", source_url="https://www.pixiv.net/users/1980643/bookmarks/artworks")
        meta = {"user": {"id": 7654321, "name": "Bookmarked Artist", "account": "bookmarked"}}
        should_link, url_c, meta_c = _should_auto_link_creator(self.provider, meta, ss)
        assert should_link is False
        assert url_c == "1980643"
        assert meta_c == "7654321"


# ── Bilibili ─────────────────────────────────────────────────────

class TestAutoLinkBilibili:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.provider = registry.get("bilibili")

    def test_match_own_article(self):
        """User's own article → should auto-link."""
        ss = FakeSubscriptionSource(source="bilibili", source_url="https://space.bilibili.com/8047632/article")
        meta = {"id": 1001, "user": {"id": 8047632, "name": "My Name"}}
        should_link, _, _ = _should_auto_link_creator(self.provider, meta, ss)
        assert should_link is True

    def test_mismatch_favlist(self):
        """Favorited article from another author → should NOT auto-link."""
        ss = FakeSubscriptionSource(
            source="bilibili",
            source_url="https://space.bilibili.com/8047632/favlist?fid=1&ftype=article",
        )
        meta = {"id": 2002, "user": {"id": 1111111, "name": "Other Author"}}
        should_link, url_c, meta_c = _should_auto_link_creator(self.provider, meta, ss)
        assert should_link is False
        assert url_c == "8047632"
        assert meta_c == "1111111"


# ── Edge Cases ───────────────────────────────────────────────────

class TestAutoLinkEdgeCases:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.provider = registry.get("x")

    def test_no_subscription_source(self):
        """No SubscriptionSource → should NOT auto-link."""
        meta = {"id_str": "123", "user": {"id": 111, "name": "user", "nick": "U"}}
        should_link, url_c, meta_c = _should_auto_link_creator(self.provider, meta, None)
        assert should_link is False
        assert url_c is None
        assert meta_c is None

    def test_no_source_url(self):
        """SubscriptionSource without URL → should NOT auto-link."""
        ss = FakeSubscriptionSource(source="x", source_url="")
        meta = {"id_str": "123", "user": {"id": 111, "name": "user", "nick": "U"}}
        should_link, url_c, meta_c = _should_auto_link_creator(self.provider, meta, ss)
        assert should_link is False
        assert url_c is None
        assert meta_c is None

    def test_unknown_provider(self):
        """Unknown source in SubscriptionSource → should NOT auto-link."""
        ss = FakeSubscriptionSource(source="nonexistent", source_url="https://example.com/user")
        meta = {"id": "123"}
        should_link, url_c, meta_c = _should_auto_link_creator(self.provider, meta, ss)
        assert should_link is False

    def test_empty_meta_creator(self):
        """Metadata with no creator info → should NOT auto-link."""
        ss = FakeSubscriptionSource(source="x", source_url="https://x.com/user")
        meta = {"id_str": "123"}  # no user field
        should_link, url_c, meta_c = _should_auto_link_creator(self.provider, meta, ss)
        assert should_link is False

    def test_multi_creator_url_returns_none(self):
        """Danbooru (no get_creator_dir_from_url) → should NOT auto-link."""
        danbooru = registry.get("danbooru")
        ss = FakeSubscriptionSource(source="danbooru", source_url="https://danbooru.donmai.us/posts?tags=ask")
        meta = {"id": 12345, "tag_string_artist": "ask"}
        should_link, url_c, meta_c = _should_auto_link_creator(danbooru, meta, ss)
        assert should_link is False
        assert url_c is None  # Danbooru returns None from base class
