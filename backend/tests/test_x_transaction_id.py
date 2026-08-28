import pytest


def _homepage_fixture():
    verification_key = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
    row = "1 2 3 4 5 6 7 8 9 10 11"
    path = "M00000000" + "C".join([row] * 16)
    frames = "".join(
        f'<svg id="loading-x-anim-{index}"><path></path><path d="{path}"></path></svg>'
        for index in range(4)
    )
    return (
        f'<meta name="twitter-site-verification" content="{verification_key}">'
        f',"ondemand.s":"fixture"{frames}'
    )


@pytest.mark.asyncio
async def test_gallery_dl_transaction_provider_refreshes_bounded_current_web_state():
    from app.remote_discovery.x_transaction import GalleryDlXTransactionIdProvider

    homepage_url = "https://x.com/"
    ondemand_url = (
        "https://abs.twimg.com/responsive-web/client-web/ondemand.s.fixturea.js"
    )
    payloads = {
        homepage_url: _homepage_fixture(),
        ondemand_url: "(a[0], 16)(a[1], 16)(a[2], 16)",
    }
    requests = []

    async def fetch_text(url):
        requests.append(url)
        return payloads[url]

    now = [100.0]
    provider = GalleryDlXTransactionIdProvider(
        fetch_text=fetch_text,
        monotonic=lambda: now[0],
        cache_ttl_seconds=10,
    )

    first = await provider.generate("GET", "https://x.com/i/api/graphql/query/Following")
    second = await provider.generate("POST", "https://api.x.com/1.1/example.json")

    assert isinstance(first, str) and first
    assert isinstance(second, str) and second
    assert requests == [homepage_url, ondemand_url]

    now[0] = 111.0
    refreshed = await provider.generate(
        "GET",
        "https://x.com/i/api/graphql/query/Following",
    )

    assert isinstance(refreshed, str) and refreshed
    assert requests == [homepage_url, ondemand_url, homepage_url, ondemand_url]
