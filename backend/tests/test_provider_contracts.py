import pytest

from app.providers import registry
from app.services.settings import extractor_key_for_source


DOWNLOADABLE_PROVIDER_URLS = {
    "pixiv": "https://www.pixiv.net/users/11",
    "x": "https://x.com/X",
    "iwara": "https://www.iwara.tv/profile/example",
    "danbooru": "https://danbooru.donmai.us/posts?tags=rating:s",
    "pinterest": "https://www.pinterest.com/pinterest/pins/",
    "lofter": "https://example.lofter.com/",
    "weibo": "https://weibo.com/u/2803301701",
    "bilibili": "https://space.bilibili.com/8047632/article",
}


@pytest.mark.parametrize("source,url", DOWNLOADABLE_PROVIDER_URLS.items())
def test_downloadable_provider_url_and_config_contract(source, url):
    provider = registry.get(source)
    assert provider.capabilities.can_download is True
    assert provider.capabilities.supports_gallerydl is True
    normalized = provider.normalize_url(url) or url
    assert provider.validate_url(normalized), source
    cfg = provider.build_gallerydl_config(None)
    extractor = extractor_key_for_source(source)
    assert extractor in cfg.get("extractor", {})


def test_unknown_provider_raises_keyerror_contract():
    with pytest.raises(KeyError):
        registry.get("not-a-provider")


@pytest.mark.parametrize(
    ("source", "url", "kind"),
    [
        ("pixiv", "https://www.pixiv.net/users/11", "creator"),
        ("pixiv", "https://www.pixiv.net/artworks/22", "work"),
        ("x", "https://twitter.com/example", "creator"),
        ("x", "https://x.com/example/status/22", "work"),
        ("iwara", "https://www.iwara.tv/profile/example", "creator"),
        ("iwara", "https://www.iwara.tv/video/example", "work"),
        ("danbooru", "https://danbooru.donmai.us/artists/11", "creator"),
        ("danbooru", "https://danbooru.donmai.us/posts/22", "work"),
        ("weibo", "https://weibo.com/u/11", "creator"),
        ("weibo", "https://weibo.com/detail/AbC22", "work"),
        ("bilibili", "https://space.bilibili.com/11", "creator"),
        ("bilibili", "https://www.bilibili.com/read/cv22", "work"),
        ("pinterest", "https://www.pinterest.com/example/pins/", "creator"),
        ("pinterest", "https://www.pinterest.com/pin/22", "work"),
        ("lofter", "https://example.lofter.com/", "creator"),
        ("lofter", "https://example.lofter.com/post/22_ab", "work"),
    ],
)
def test_provider_search_urls_are_classified_without_network(source, url, kind):
    parsed = registry.get(source).parse_search_url(url)
    assert parsed is not None
    assert parsed.kind == kind
    assert parsed.normalized_url.startswith("https://")
