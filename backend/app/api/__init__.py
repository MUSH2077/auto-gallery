from fastapi import APIRouter

from app.api.system import router as system_router
from app.api.system import tasks_ops_router as system_tasks_ops_router
from app.api.creators import router as creators_router
from app.api.subscriptions import router as subscriptions_router
from app.api.download_jobs import router as download_jobs_router
from app.api.creators import curation_router as creators_curation_router
from app.api.works import router as works_router
from app.api.works import curation_router as works_curation_router
from app.api.tags import router as tags_router
from app.api.tags import curation_router as tags_curation_router
from app.api.search import router as search_router
from app.api.admin import router as admin_router
from app.api.admin import curation_ops_router as admin_curation_ops_router
from app.api.admin import tasks_ops_router as admin_tasks_ops_router
from app.api.sources import router as sources_router
from app.api.reference import router as reference_router
from app.api.import_jobs import router as import_jobs_router
from app.api.auth_api import router as auth_router
from app.api.repositories import router as repositories_router
from app.api.curation import router as curation_router
from app.api.ws import router as ws_router
from app.api.tasks import router as tasks_router
from app.api.operations import router as operations_router
from app.api.users import router as users_router
from app.api.upload import router as upload_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(ws_router, tags=["websocket"])
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(system_router, tags=["system"])
api_router.include_router(system_tasks_ops_router, tags=["system"])
api_router.include_router(sources_router, prefix="/sources", tags=["sources"])
api_router.include_router(reference_router, prefix="/reference", tags=["reference"])
api_router.include_router(creators_router, prefix="/creators", tags=["creators"])
api_router.include_router(creators_curation_router, prefix="/creators", tags=["creators"])
api_router.include_router(subscriptions_router, prefix="/subscriptions", tags=["subscriptions"])
api_router.include_router(download_jobs_router, prefix="/download-jobs", tags=["download-jobs"])
api_router.include_router(import_jobs_router, prefix="/import-jobs", tags=["import-jobs"])
api_router.include_router(tasks_router, prefix="/tasks", tags=["tasks"])
api_router.include_router(operations_router, prefix="/operations", tags=["operations"])
api_router.include_router(repositories_router, prefix="/repositories", tags=["repositories"])
api_router.include_router(curation_router, prefix="/curation", tags=["curation"])
api_router.include_router(works_router, prefix="/works", tags=["works"])
api_router.include_router(works_curation_router, prefix="/works", tags=["works"])
api_router.include_router(tags_router, prefix="/tags", tags=["tags"])
api_router.include_router(tags_curation_router, prefix="/tags", tags=["tags"])
api_router.include_router(search_router, prefix="/search", tags=["search"])
api_router.include_router(admin_router, prefix="/admin", tags=["admin"])
api_router.include_router(admin_curation_ops_router, prefix="/admin", tags=["admin"])
api_router.include_router(admin_tasks_ops_router, prefix="/admin", tags=["admin"])
api_router.include_router(users_router, prefix="/users", tags=["users"])
api_router.include_router(upload_router, prefix="/upload", tags=["upload"])
