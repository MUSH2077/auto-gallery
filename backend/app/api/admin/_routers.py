from fastapi import APIRouter

from app.auth import RequirePermission
from app.services import admin_data

router = APIRouter(dependencies=[RequirePermission("system")])
curation_ops_router = APIRouter(dependencies=[RequirePermission("curation")])
tasks_ops_router = APIRouter(dependencies=[RequirePermission("tasks")])
_clear_files = admin_data._clear_files
