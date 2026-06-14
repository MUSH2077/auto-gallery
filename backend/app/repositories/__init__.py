"""Repository package.

Keep this module lightweight. Import concrete repositories from their own
modules to avoid circular imports during RQ job loading.

Public API (import from sub-modules)::

    from app.repositories.creator import CreatorRepository
    from app.repositories.work import WorkRepository
    from app.repositories.subscription import SubscriptionRepository
    from app.repositories.download_job import DownloadJobRepository
    from app.repositories.tag import TagRepository
"""

__all__ = [
    "CreatorRepository",
    "WorkRepository",
    "SubscriptionRepository",
    "DownloadJobRepository",
    "TagRepository",
]
