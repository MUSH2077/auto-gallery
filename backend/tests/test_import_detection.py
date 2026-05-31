"""Tests for AI and NSFW detection functions in import_runner."""

from app.jobs.import_runner import _detect_ai_generated, _detect_nsfw


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

    def test_pixiv_sanity_7_is_nsfw(self):
        assert _detect_nsfw({"sanity_level": 7}, "pixiv") is True

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
