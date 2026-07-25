from ._routers import router, curation_ops_router, tasks_ops_router
from . import settings, data, scheduler, gallerydl, dedup, backup, auth_health  # noqa: F401

router.include_router(curation_ops_router)
router.include_router(tasks_ops_router)
