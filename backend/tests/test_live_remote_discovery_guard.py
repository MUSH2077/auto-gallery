"""The normal suite must never contact remote discovery providers."""

from tests.live.remote_discovery_guard import live_provider_specs


def test_live_provider_specs_require_exact_opt_in_and_dedicated_credentials():
    secret = "live-secret-canary-that-must-not-render"
    assert live_provider_specs({"LIVE_PIXIV_REFRESH_TOKEN": secret}) == ()
    assert live_provider_specs(
        {
            "AUTO_GALLERY_LIVE_REMOTE_DISCOVERY": "yes",
            "LIVE_PIXIV_REFRESH_TOKEN": secret,
        }
    ) == ()

    specs = live_provider_specs(
        {
            "AUTO_GALLERY_LIVE_REMOTE_DISCOVERY": "explicitly-enabled",
            "LIVE_PIXIV_REFRESH_TOKEN": secret,
        }
    )
    assert [spec.source for spec in specs] == ["pixiv"]
    assert secret not in repr(specs)
