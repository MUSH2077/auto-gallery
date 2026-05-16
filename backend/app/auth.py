from fastapi import Header, HTTPException, Depends

from app.config import settings


async def get_admin_key(x_admin_key: str = Header(default="", alias="X-Admin-Key")):
    """Validate X-Admin-Key header against configured admin password."""
    if not x_admin_key or x_admin_key != settings.admin_password:
        raise HTTPException(status_code=401, detail="Invalid or missing admin API key")
    return x_admin_key


# Dependency alias
RequireAdmin = Depends(get_admin_key)
