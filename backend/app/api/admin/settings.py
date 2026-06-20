"""Admin settings endpoints — CRUD, reset, download/subscription defaults."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequireAdmin
from app.database import get_db

router = APIRouter(dependencies=[RequireAdmin])

# Routes imported from the main admin module during incremental migration.
# All settings routes remain in admin.py for now; import them via __init__.py.
