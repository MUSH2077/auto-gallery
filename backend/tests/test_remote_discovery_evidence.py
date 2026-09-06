from datetime import UTC, datetime, timedelta


def test_multilingual_artist_bios_are_detected_without_generic_fandom_text():
    """Removing a language rule or matching generic fandom text would corrupt confidence tiers."""
    from app.remote_discovery.evidence import art_focused_bio

    assert art_focused_bio("Illustrator / concept artist. Commissions open") is True
    assert art_focused_bio("イラスト・原画を描いています") is True
    assert art_focused_bio("自由插画师，约稿请私信") is True
    assert art_focused_bio("喜欢漫画、游戏和旅行") is False


def test_supported_links_require_expanded_known_art_or_provider_urls():
    """Treating shorteners or arbitrary homepages as evidence would create false high matches."""
    from app.remote_discovery.evidence import supported_profile_links

    assert supported_profile_links([
        "https://www.pixiv.net/users/123",
        "https://danbooru.donmai.us/artists/456",
        "https://x.com/example",
    ], source="bilibili") == (
        "https://www.pixiv.net/users/123",
        "https://danbooru.donmai.us/artists/456",
        "https://x.com/example",
    )
    assert supported_profile_links([
        "https://t.co/opaque",
        "https://portfolio.example/artist",
        "https://space.bilibili.com/123/dynamic",
    ], source="bilibili") == ()


def test_expanded_links_can_match_verified_creator_links_but_drop_shorteners():
    from app.remote_discovery.evidence import expanded_profile_links

    assert expanded_profile_links([
        "https://portfolio.example/artist",
        "https://t.co/opaque",
        "https://b23.tv/opaque",
    ]) == ("https://portfolio.example/artist",)


def test_recent_visual_evidence_uses_inclusive_ninety_day_boundary():
    """Changing the age boundary or accepting media-free posts would alter automatic imports."""
    from app.remote_discovery.evidence import has_recent_visual_post

    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    assert has_recent_visual_post(
        [{"created_at": now - timedelta(days=90), "has_visual_media": True}],
        now=now,
    ) is True
    assert has_recent_visual_post(
        [{"created_at": now - timedelta(days=90, seconds=1), "has_visual_media": True}],
        now=now,
    ) is False
    assert has_recent_visual_post(
        [{"created_at": now - timedelta(days=1), "has_visual_media": False}],
        now=now,
    ) is False
