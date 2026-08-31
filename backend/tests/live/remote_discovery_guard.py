"""Secret-safe opt-in policy for live provider smoke tests."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


LIVE_OPT_IN_ENV = "AUTO_GALLERY_LIVE_REMOTE_DISCOVERY"
LIVE_OPT_IN_VALUE = "explicitly-enabled"


@dataclass(frozen=True)
class LiveProviderSpec:
    source: str
    auth_method: str
    credentials: dict[str, str] = field(repr=False)


def live_provider_specs(environ: dict[str, str] | None = None) -> tuple[LiveProviderSpec, ...]:
    env = os.environ if environ is None else environ
    if env.get(LIVE_OPT_IN_ENV) != LIVE_OPT_IN_VALUE:
        return ()

    specs: list[LiveProviderSpec] = []
    if token := env.get("LIVE_PIXIV_REFRESH_TOKEN"):
        specs.append(LiveProviderSpec("pixiv", "refresh_token", {"refresh_token": token}))
    if cookie := env.get("LIVE_X_COOKIE"):
        specs.append(LiveProviderSpec("x", "cookie", {"cookie": cookie}))
    if sessdata := env.get("LIVE_BILIBILI_SESSDATA"):
        specs.append(LiveProviderSpec("bilibili", "sessdata", {"SESSDATA": sessdata}))
    return tuple(specs)
