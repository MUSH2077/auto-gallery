from collections import deque

import pytest


class FixtureBootstrapTransport:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.requests = []

    async def fetch(self, url, *, headers):
        self.requests.append((url, headers))
        if not self.responses:
            raise AssertionError("unexpected X transaction bootstrap request")
        return self.responses.popleft()


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
    from app.remote_discovery.x_transaction import (
        GalleryDlXTransactionIdProvider,
        XTransactionBootstrapResponse,
    )

    homepage_url = "https://x.com/"
    ondemand_url = (
        "https://abs.twimg.com/responsive-web/client-web/ondemand.s.fixturea.js"
    )
    transport = FixtureBootstrapTransport(
        XTransactionBootstrapResponse(200, _homepage_fixture(), {}),
        XTransactionBootstrapResponse(
            200,
            "(a[0], 16)(a[1], 16)(a[2], 16)",
            {},
        ),
        XTransactionBootstrapResponse(200, _homepage_fixture(), {}),
        XTransactionBootstrapResponse(
            200,
            "(a[0], 16)(a[1], 16)(a[2], 16)",
            {},
        ),
    )

    now = [100.0]
    provider = GalleryDlXTransactionIdProvider(
        bootstrap_transport=transport,
        monotonic=lambda: now[0],
        cache_ttl_seconds=10,
    )
    raw_cookie = "auth_token=bootstrap-secret; ct0=bootstrap-csrf"

    first = await provider.generate(
        "GET",
        "https://x.com/i/api/graphql/query/Following",
        cookie=raw_cookie,
    )
    second = await provider.generate(
        "POST",
        "https://api.x.com/1.1/example.json",
        cookie=raw_cookie,
    )

    assert isinstance(first, str) and first
    assert isinstance(second, str) and second
    assert [request[0] for request in transport.requests] == [homepage_url, ondemand_url]
    assert [request[1]["Cookie"] for request in transport.requests] == [
        raw_cookie,
        raw_cookie,
    ]
    assert raw_cookie not in repr(provider)

    now[0] = 111.0
    refreshed = await provider.generate(
        "GET",
        "https://x.com/i/api/graphql/query/Following",
        cookie=raw_cookie,
    )

    assert isinstance(refreshed, str) and refreshed
    assert [request[0] for request in transport.requests] == [
        homepage_url,
        ondemand_url,
        homepage_url,
        ondemand_url,
    ]


@pytest.mark.asyncio
async def test_x_web_anonymous_app_state_fails_closed_without_transaction_material():
    from app.remote_discovery.common import RemoteDiscoveryError
    from app.remote_discovery.x_transaction import (
        GalleryDlXTransactionIdProvider,
        XTransactionBootstrapResponse,
    )

    raw_cookie = "auth_token=expired-secret; ct0=expired-csrf"
    response = XTransactionBootstrapResponse(
        200,
        '<html><script src="/x-web/client-web/main.js"></script></html>',
        {"Set-Cookie": raw_cookie},
    )
    provider = GalleryDlXTransactionIdProvider(
        bootstrap_transport=FixtureBootstrapTransport(response)
    )

    with pytest.raises(
        RemoteDiscoveryError,
        match="authenticated.*legacy responsive-web.*transaction",
    ) as caught:
        await provider.generate(
            "GET",
            "https://x.com/i/api/graphql/query/Following",
            cookie=raw_cookie,
        )

    assert raw_cookie not in str(caught.value)
    assert raw_cookie not in repr(response)
    assert raw_cookie not in repr(provider)


@pytest.mark.asyncio
async def test_x_transaction_bootstrap_preserves_retry_after_without_secret_leak():
    from app.remote_discovery.common import RemoteRateLimited
    from app.remote_discovery.x_transaction import (
        GalleryDlXTransactionIdProvider,
        XTransactionBootstrapResponse,
    )

    raw_cookie = "auth_token=rate-secret; ct0=rate-csrf"
    response = XTransactionBootstrapResponse(
        429,
        "rate-secret response body",
        {"Retry-After": "27", "Set-Cookie": raw_cookie},
    )
    provider = GalleryDlXTransactionIdProvider(
        bootstrap_transport=FixtureBootstrapTransport(response)
    )

    with pytest.raises(RemoteRateLimited) as caught:
        await provider.generate(
            "GET",
            "https://x.com/i/api/graphql/query/Following",
            cookie=raw_cookie,
        )

    assert caught.value.retry_after_seconds == 27
    assert raw_cookie not in str(caught.value)
    assert raw_cookie not in repr(response)
    assert "rate-secret response body" not in repr(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_x_transaction_bootstrap_maps_authentication_failures(status):
    from app.remote_discovery.common import RemoteReauthenticationRequired
    from app.remote_discovery.x_transaction import (
        GalleryDlXTransactionIdProvider,
        XTransactionBootstrapResponse,
    )

    provider = GalleryDlXTransactionIdProvider(
        bootstrap_transport=FixtureBootstrapTransport(
            XTransactionBootstrapResponse(status, "unauthorized", {})
        )
    )

    with pytest.raises(RemoteReauthenticationRequired) as caught:
        await provider.generate(
            "GET",
            "https://x.com/i/api/graphql/query/Following",
            cookie="auth_token=expired; ct0=expired",
        )

    assert caught.value.status_code == status
