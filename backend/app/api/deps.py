from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

DBSession = Depends(get_db)
