import pytest

from app.providers import registry as provider_registry


@pytest.mark.parametrize(
    ("source", "auth_methods"),
    [
        ("pixiv", ("refresh_token",)),
        ("x", ("oauth2", "cookie")),
        ("bilibili", ("sessdata",)),
    ],
)
def test_remote_provider_capabilities_advertise_auth_and_collection_selectors(source, auth_methods):
    capabilities = provider_registry.get(source).capabilities

    assert capabilities.supports_remote_discovery is True
    assert capabilities.discovery_auth_methods == auth_methods
    assert capabilities.supports_collection_selectors is True


def test_unrelated_provider_retains_non_discovery_defaults():
    capabilities = provider_registry.get("iwara").capabilities

    assert capabilities.can_download is True
    assert capabilities.supports_gallerydl is True
    assert capabilities.supports_remote_discovery is False
    assert capabilities.discovery_auth_methods == ()
    assert capabilities.supports_collection_selectors is False


@pytest.mark.parametrize(
    ("url", "normalized"),
    [
        (
            "https://space.bilibili.com/765/dynamic",
            "https://space.bilibili.com/765/dynamic",
        ),
        (
            "https://space.bilibili.com/765/upload/opus",
            "https://space.bilibili.com/765/upload/opus",
        ),
    ],
)
def test_bilibili_discovered_account_urls_preserve_dynamic_and_opus_contracts(url, normalized):
    provider = provider_registry.get("bilibili")

    assert provider.normalize_url(url) == normalized
    assert provider.validate_url(normalized) is True
    parsed = provider.parse_search_url(url)
    assert parsed is not None
    assert parsed.kind == "creator"
    assert parsed.normalized_url == normalized


def test_default_discovery_registry_resolves_all_supported_sources():
    from app.remote_discovery import registry

    assert tuple(registry.get(source).source for source in ("pixiv", "x", "bilibili")) == (
        "pixiv",
        "x",
        "bilibili",
    )


@pytest.mark.asyncio
async def test_source_capability_api_exposes_remote_discovery_contract():
    from app.api.sources import list_sources

    payload = await list_sources()
    pixiv = next(item for item in payload["sources"] if item["source_name"] == "pixiv")

    assert pixiv["capabilities"]["supports_remote_discovery"] is True
    assert pixiv["capabilities"]["discovery_auth_methods"] == ("refresh_token",)
    assert pixiv["capabilities"]["supports_collection_selectors"] is True
