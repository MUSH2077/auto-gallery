from app.providers.registry import registry, ProviderRegistry
from app.providers.pixiv import PixivProvider
from app.providers.iwara import IwaraProvider
from app.providers.x import XProvider
from app.providers.danbooru_reference import DanbooruReferenceProvider
from app.providers.danbooru import DanbooruProvider
from app.providers.pinterest import PinterestProvider
from app.providers.lofter import LofterProvider
from app.providers.weibo import WeiboProvider
from app.providers.local import LocalProvider
from app.providers.manual import ManualProvider

registry.register(PixivProvider())
registry.register(IwaraProvider())
registry.register(XProvider())
registry.register(DanbooruProvider())
registry.register(DanbooruReferenceProvider())
registry.register(PinterestProvider())
registry.register(LofterProvider())
registry.register(WeiboProvider())
registry.register(LocalProvider())
registry.register(ManualProvider())

__all__ = ["registry", "ProviderRegistry"]
