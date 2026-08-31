from app.remote_discovery.classifier import (
    IdentityClassification,
    IdentityMatchSignals,
    classify_identity,
)
from app.remote_discovery.contract import (
    DiscoveryPage,
    RemoteCandidateIdentity,
    RemoteCollection,
    RemoteCreatorDetail,
    RemoteCreatorProfile,
    RemoteDiscoveryAdapter,
    RemoteWorkPage,
    RemoteWorkPreview,
)
from app.remote_discovery.registry import DiscoveryAdapterRegistry, registry


def init_discovery_adapters() -> None:
    from app.remote_discovery.bilibili import BilibiliRemoteDiscoveryAdapter
    from app.remote_discovery.pixiv import PixivRemoteDiscoveryAdapter
    from app.remote_discovery.x import XRemoteDiscoveryAdapter

    for adapter in (
        PixivRemoteDiscoveryAdapter(),
        XRemoteDiscoveryAdapter(),
        BilibiliRemoteDiscoveryAdapter(),
    ):
        registry.register(adapter)


init_discovery_adapters()

__all__ = [
    "DiscoveryAdapterRegistry",
    "DiscoveryPage",
    "IdentityClassification",
    "IdentityMatchSignals",
    "RemoteCandidateIdentity",
    "RemoteCollection",
    "RemoteCreatorDetail",
    "RemoteCreatorProfile",
    "RemoteDiscoveryAdapter",
    "RemoteWorkPage",
    "RemoteWorkPreview",
    "classify_identity",
    "init_discovery_adapters",
    "registry",
]
