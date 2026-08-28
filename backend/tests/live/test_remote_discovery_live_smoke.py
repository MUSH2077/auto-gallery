"""Optional real-account smoke tests; never collected into network use by default."""

from __future__ import annotations

import pytest

from app.remote_discovery.registry import registry
from app.services.remote_credentials import RedactedCredentials
from tests.live.remote_discovery_guard import live_provider_specs


SPECS = live_provider_specs()

pytestmark = [
    pytest.mark.live_provider,
    pytest.mark.skipif(
        not SPECS,
        reason=(
            "live remote discovery disabled; set the exact opt-in plus dedicated "
            "LIVE_<PROVIDER> test credentials"
        ),
    ),
]


@pytest.mark.parametrize("spec", SPECS, ids=lambda spec: spec.source)
@pytest.mark.asyncio
async def test_live_provider_account_lists_one_discovery_page(spec):
    """Validate the dedicated test account and one bounded page without logging secrets."""

    adapter = registry.get(spec.source)
    credentials = RedactedCredentials(
        {**spec.credentials, "auth_method": spec.auth_method}
    )
    try:
        identity = await adapter.validate_account(credentials)
        collections = await adapter.list_collections(credentials)
        selector = collections[0].selector if collections else None
        page = await adapter.fetch_page(
            credentials,
            selector=selector,
            cursor=None,
            page_size=1,
        )
        assert identity.source == spec.source
        assert len(page.items) <= 1
    except Exception as exc:  # pragma: no cover - runs only with operator opt-in
        pytest.fail(
            f"{spec.source} live smoke failed ({type(exc).__name__})",
            pytrace=False,
        )
