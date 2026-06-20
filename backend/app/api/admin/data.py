"""Admin endpoint group — migrated from admin.py during incremental split."""
from fastapi import APIRouter, Depends
from app.auth import RequireAdmin
router = APIRouter(dependencies=[RequireAdmin])
