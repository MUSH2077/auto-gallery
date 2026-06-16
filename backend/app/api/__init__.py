from fastapi import APIRouter

from app.api.system import router as system_router
from app.api.creators import router as creators_router
from app.api.subscriptions import router as subscriptions_router
from app.api.download_jobs import router as download_jobs_router
from app.api.works import router as works_router
from app.api.tags import router as tags_router
from app.api.search import router as search_router
from app.api.admin import router as admin_router
from app.api.sources import router as sources_router
from app.api.reference import router as reference_router
from app.api.import_jobs import router as import_jobs_router
from app.api.auth_api import router as auth_router
from app.api.repositories import router as repositories_router
from app.api.curation import router as curation_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(system_router, tags=["system"])
api_router.include_router(sources_router, prefix="/sources", tags=["sources"])
api_router.include_router(reference_router, prefix="/reference", tags=["reference"])
api_router.include_router(creators_router, prefix="/creators", tags=["creators"])
api_router.include_router(subscriptions_router, prefix="/subscriptions", tags=["subscriptions"])
api_router.include_router(download_jobs_router, prefix="/download-jobs", tags=["download-jobs"])
api_router.include_router(import_jobs_router, prefix="/import-jobs", tags=["import-jobs"])
api_router.include_router(repositories_router, prefix="/repositories", tags=["repositories"])
api_router.include_router(curation_router, prefix="/curation", tags=["curation"])
api_router.include_router(works_router, prefix="/works", tags=["works"])
api_router.include_router(tags_router, prefix="/tags", tags=["tags"])
api_router.include_router(search_router, prefix="/search", tags=["search"])
api_router.include_router(admin_router, prefix="/admin", tags=["admin"])
