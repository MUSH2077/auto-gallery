"""Tests for AI and NSFW detection functions in import_runner."""

from app.jobs.import_runner import (
    ASSET_EXTENSIONS,
    _can_compute_phash,
    _can_generate_thumbnail,
    _detect_ai_generated,
    _detect_nsfw,
    _mime_type,
)


class TestDetectAIGenerated:
    def test_pixiv_ai_type_2_is_ai(self):
        assert _detect_ai_generated({"illust_ai_type": 2}, "pixiv") is True

    def test_pixiv_ai_type_1_is_human(self):
        assert _detect_ai_generated({"illust_ai_type": 1}, "pixiv") is False

    def test_pixiv_ai_type_0_is_human(self):
        assert _detect_ai_generated({"illust_ai_type": 0}, "pixiv") is False

    def test_pixiv_no_ai_type_is_human(self):
        assert _detect_ai_generated({}, "pixiv") is False

    def test_danbooru_ai_generated_tag(self):
        assert _detect_ai_generated({"tag_string_meta": "ai_generated highres"}, "danbooru") is True

    def test_danbooru_no_ai_tag(self):
        assert _detect_ai_generated({"tag_string_meta": "highres"}, "danbooru") is False

    def test_generic_ai_indicators(self):
        assert _detect_ai_generated({"tags": "ai_generated art"}, "other") is True
        assert _detect_ai_generated({"description": "created by ai"}, "other") is True

    def test_generic_no_ai(self):
        assert _detect_ai_generated({"tags": "original art"}, "other") is False


class TestDetectNSFW:
    def test_pixiv_restrict_1_is_nsfw(self):
        assert _detect_nsfw({"restrict": 1}, "pixiv") is True

    def test_pixiv_restrict_2_is_nsfw(self):
        assert _detect_nsfw({"restrict": 2}, "pixiv") is True

    def test_pixiv_restrict_0_is_safe(self):
        assert _detect_nsfw({"restrict": 0}, "pixiv") is False

    def test_pixiv_sanity_7_is_not_nsfw_by_sanity_alone(self):
        # sanity_level is intentionally excluded from detection per CLAUDE.md:
        # "sanity_level: 0-11, 6+ restricted, 7+ explicit (NOT reliable alone)"
        # Detection uses rating (from x_restrict) and x_restrict only.
        assert _detect_nsfw({"sanity_level": 7}, "pixiv") is False

    def test_pixiv_sanity_4_is_safe(self):
        assert _detect_nsfw({"sanity_level": 4}, "pixiv") is False

    def test_danbooru_rating_q_is_nsfw(self):
        assert _detect_nsfw({"rating": "q"}, "danbooru") is True

    def test_danbooru_rating_e_is_nsfw(self):
        assert _detect_nsfw({"rating": "e"}, "danbooru") is True

    def test_danbooru_rating_s_is_safe(self):
        assert _detect_nsfw({"rating": "s"}, "danbooru") is False

    def test_iwara_ecchi_is_nsfw(self):
        assert _detect_nsfw({"rating": "ecchi"}, "iwara") is True

    def test_x_sensitive(self):
        assert _detect_nsfw({"possibly_sensitive": True}, "x") is True

    def test_unknown_source_safe(self):
        assert _detect_nsfw({"rating": "e"}, "unknown") is False


def test_provider_specific_ai_signals_keep_generic_historical_fallbacks():
    from app.providers import registry

    pixiv = registry.get("pixiv")
    danbooru = registry.get("danbooru")
    assert pixiv.parse_work_flags({"illust_ai_type": 1, "description": "created by AI"})["is_ai_generated"] is True
    assert danbooru.parse_work_flags({"rating": "s", "artist_commentary_desc": "AI art"})["is_ai_generated"] is True
    assert pixiv.parse_work_flags({"illust_ai_type": 1})["is_ai_generated"] is False
    assert danbooru.parse_work_flags({"rating": "s"})["is_ai_generated"] is False


class TestUgoiraAssetHandling:
    def test_gif_is_previewable_asset_without_phash(self):
        assert ".gif" in ASSET_EXTENSIONS
        assert _mime_type(".gif") == "image/gif"
        assert _can_generate_thumbnail(".gif") is True
        assert _can_compute_phash(".gif") is False

    def test_zip_is_archive_asset_without_thumbnail_or_phash(self):
        assert ".zip" in ASSET_EXTENSIONS
        assert _mime_type(".zip") == "application/zip"
        assert _can_generate_thumbnail(".zip") is False
        assert _can_compute_phash(".zip") is False
