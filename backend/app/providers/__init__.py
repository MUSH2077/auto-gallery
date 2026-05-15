from app.providers.registry import registry, ProviderRegistry
from app.providers.pixiv import PixivProvider
from app.providers.iwara import IwaraProvider
from app.providers.x import XProvider
from app.providers.danbooru_reference import DanbooruReferenceProvider
from app.providers.local import LocalProvider
from app.providers.manual import ManualProvider

registry.register(PixivProvider())
registry.register(IwaraProvider())
registry.register(XProvider())
registry.register(DanbooruReferenceProvider())
registry.register(LocalProvider())
registry.register(ManualProvider())

__all__ = ["registry", "ProviderRegistry"]
