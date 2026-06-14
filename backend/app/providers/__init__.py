from app.providers.registry import registry, ProviderRegistry

__all__ = ["registry", "ProviderRegistry", "init_providers"]


def init_providers():
    """Register all source providers on the global registry.

    Called from the FastAPI lifespan to avoid import-time side effects.
    Safe to call multiple times — duplicate registrations are no-ops.
    """
    from app.providers.pixiv import PixivProvider
    from app.providers.iwara import IwaraProvider
    from app.providers.x import XProvider
    from app.providers.danbooru_reference import DanbooruReferenceProvider
    from app.providers.danbooru import DanbooruProvider
    from app.providers.pinterest import PinterestProvider
    from app.providers.lofter import LofterProvider
    from app.providers.weibo import WeiboProvider
    from app.providers.bilibili import BilibiliProvider
    from app.providers.local import LocalProvider
    from app.providers.manual import ManualProvider

    _providers = [
        PixivProvider(),
        IwaraProvider(),
        XProvider(),
        DanbooruProvider(),
        DanbooruReferenceProvider(),
        PinterestProvider(),
        LofterProvider(),
        WeiboProvider(),
        BilibiliProvider(),
        LocalProvider(),
        ManualProvider(),
    ]
    for p in _providers:
        try:
            registry.register(p)
        except Exception:
            pass  # Already registered


# Auto-register on import for backward compatibility with existing code
# that imports app.providers and expects providers to be available.
init_providers()
