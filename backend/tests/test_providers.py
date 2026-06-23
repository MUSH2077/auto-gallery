"""Unit tests for source provider parse methods."""

from app.providers.pixiv import PixivProvider
from app.providers.iwara import IwaraProvider
from app.providers.x import XProvider


class TestPixivProvider:
    def setup_method(self):
        self.p = PixivProvider()

    def test_source_name(self):
        assert self.p.source_name == "pixiv"

    def test_capabilities(self):
        c = self.p.capabilities
        assert c.can_download is True
        assert c.supports_gallerydl is True

    def test_validate_url_artworks(self):
        assert self.p.validate_url("https://www.pixiv.net/en/artworks/38362603")
        assert self.p.validate_url("https://www.pixiv.net/artworks/38362603")

    def test_validate_url_users(self):
        assert self.p.validate_url("https://www.pixiv.net/en/users/1980643")

    def test_validate_url_invalid(self):
        assert not self.p.validate_url("https://twitter.com/user")
        assert not self.p.validate_url("https://iwara.tv/video/123")

    def test_normalize_url(self):
        result = self.p.normalize_url("https://www.pixiv.net/en/artworks/38362603")
        assert result == "https://www.pixiv.net/artworks/38362603"

    def test_parse_source_creator(self, sample_pixiv_metadata):
        result = self.p.parse_source_creator(sample_pixiv_metadata)
        assert result["source"] == "pixiv"
        assert result["source_creator_id"] == "1980643"
        assert result["display_name"] == "ASK"
        assert "pixiv.net/users/1980643" in result["source_url"]

    def test_parse_work_source(self, sample_pixiv_metadata):
        result = self.p.parse_work_source(sample_pixiv_metadata)
        assert result["source"] == "pixiv"
        assert result["source_work_id"] == "38362603"
        assert result["title"] == "Test Illustration"
        assert "artworks/38362603" in result["source_url"]

    def test_parse_assets_multi_page(self, sample_pixiv_metadata):
        result = self.p.parse_assets(sample_pixiv_metadata, [])
        assert len(result) == 2
        assert result[0]["width"] == 800
        assert result[0]["height"] == 600

    def test_parse_source_tags(self, sample_pixiv_metadata):
        result = self.p.parse_source_tags(sample_pixiv_metadata)
        assert len(result) == 2
        tag_names = [t["original_name"] for t in result]
        assert "original" in tag_names
        assert "test_tag" in tag_names


class TestIwaraProvider:
    def setup_method(self):
        self.p = IwaraProvider()

    def test_source_name(self):
        assert self.p.source_name == "iwara"

    def test_capabilities(self):
        c = self.p.capabilities
        assert c.can_download is True
        assert c.supports_gallerydl is True

    def test_validate_url(self):
        assert self.p.validate_url("https://www.iwara.tv/video/abc123")
        assert self.p.validate_url("https://www.iwara.tv/profile/testuser")
        assert not self.p.validate_url("https://pixiv.net/users/123")

    def test_parse_source_creator(self, sample_iwara_metadata):
        result = self.p.parse_source_creator(sample_iwara_metadata)
        assert result["source"] == "iwara"
        assert result["source_creator_id"] == "user123"
        assert result["display_name"] == "Test User"
        assert "profile/" in result["source_url"]

    def test_parse_work_source(self, sample_iwara_metadata):
        result = self.p.parse_work_source(sample_iwara_metadata)
        assert result["source"] == "iwara"
        assert result["source_work_id"] == "abc123"
        assert result["title"] == "Test Video"
        assert "video/abc123" in result["source_url"]

    def test_parse_assets(self, sample_iwara_metadata):
        result = self.p.parse_assets(sample_iwara_metadata, [])
        assert len(result) == 1
        assert result[0]["source"] == "iwara"
        assert result[0]["width"] == 1920
        assert result[0]["height"] == 1080

    def test_parse_source_tags(self, sample_iwara_metadata):
        result = self.p.parse_source_tags(sample_iwara_metadata)
        tag_names = [t["original_name"] for t in result]
        assert "tag1" in tag_names
        assert "tag2" in tag_names
        assert "rating:ecchi" in tag_names


class TestXProvider:
    def setup_method(self):
        self.p = XProvider()

    def test_source_name(self):
        assert self.p.source_name == "x"

    def test_capabilities(self):
        c = self.p.capabilities
        assert c.can_download is True
        assert c.supports_gallerydl is True
        assert c.supports_tags is True

    def test_validate_url(self):
        assert self.p.validate_url("https://x.com/artist_handle")
        assert self.p.validate_url("https://x.com/artist_handle/status/1234567890123456789")
        assert self.p.validate_url("https://twitter.com/artist_handle")
        assert not self.p.validate_url("https://pixiv.net/users/123")

    def test_parse_source_creator(self, sample_twitter_metadata):
        result = self.p.parse_source_creator(sample_twitter_metadata)
        assert result["source"] == "x"
        assert result["source_creator_id"] == "111222333"
        assert result["display_name"] == "Artist Name"
        assert "x.com/artist_handle" in result["source_url"]

    def test_parse_work_source(self, sample_twitter_metadata):
        result = self.p.parse_work_source(sample_twitter_metadata)
        assert result["source"] == "x"
        assert result["source_work_id"] == "1234567890123456789"
        assert result["title"] == "Check out this artwork! #fanart"
        assert "status/1234567890123456789" in result["source_url"]

    def test_parse_assets_with_entities(self, sample_twitter_metadata):
        result = self.p.parse_assets(sample_twitter_metadata, [])
        assert len(result) == 1
        assert result[0]["source_asset_id"] == "media_001"
        assert "twimg.com" in result[0]["source_url"]

    def test_parse_assets_no_entities(self):
        meta = {"id_str": "123", "url": "https://example.com/img.jpg"}
        result = self.p.parse_assets(meta, [])
        assert len(result) == 1
        assert result[0]["source_url"] == "https://example.com/img.jpg"

    def test_parse_source_tags(self, sample_twitter_metadata):
        result = self.p.parse_source_tags(sample_twitter_metadata)
        tag_names = [(t["original_name"], t["category"]) for t in result]
        assert ("fanart", "hashtag") in tag_names
        assert ("illustration", "hashtag") in tag_names

    def test_get_creator_dir_from_url(self):
        """get_creator_dir_from_url extracts screen name from profile URL."""
        assert self.p.get_creator_dir_from_url("https://x.com/artist_handle") == "artist_handle"
        assert self.p.get_creator_dir_from_url("https://twitter.com/other_user") == "other_user"
        # Status URLs also match (the regex captures the screen name)
        assert self.p.get_creator_dir_from_url("https://x.com/user/status/123") == "user"
        # Non-matching URL
        assert self.p.get_creator_dir_from_url("https://pixiv.net/users/123") is None

    def test_get_creator_directory_name(self, sample_twitter_metadata):
        """get_creator_directory_name returns the screen name from metadata."""
        assert self.p.get_creator_directory_name(sample_twitter_metadata) == "artist_handle"

    def test_creator_identity_match_own_tweet(self, sample_twitter_metadata):
        """Own tweet: URL creator and metadata creator should match."""
        url_creator = self.p.get_creator_dir_from_url("https://x.com/artist_handle")
        meta_creator = self.p.get_creator_directory_name(sample_twitter_metadata)
        assert url_creator == meta_creator == "artist_handle"

    def test_creator_identity_mismatch_retweet(self):
        """Retweet: URL creator differs from metadata creator — should NOT match."""
        retweet_meta = {
            "id_str": "999",
            "full_text": "RT of someone else",
            "user": {"id": 999888777, "name": "other_artist", "nick": "Other Artist"},
        }
        url_creator = self.p.get_creator_dir_from_url("https://x.com/artist_handle")
        meta_creator = self.p.get_creator_directory_name(retweet_meta)
        assert url_creator != meta_creator
        assert url_creator == "artist_handle"
        assert meta_creator == "other_artist"
