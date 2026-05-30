import bcrypt as _bcrypt
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.config import settings

# ── Password hashing ──────────────────────────────────────────────────────────
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_access_token(username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": username, "exp": expire},
        settings.secret_key,
        algorithm=ALGORITHM,
    )


def decode_access_token(token: str) -> Optional[str]:
    """Return username from token, or None if invalid/expired."""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


# ── Main auth dependency ──────────────────────────────────────────────────────

async def get_admin_key(
    x_admin_key: str = Header(default="", alias="X-Admin-Key"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    """Accept either Bearer JWT token or X-Admin-Key (legacy proxy injection)."""
    # 1. JWT Bearer token (from browser login)
    if credentials and credentials.credentials:
        username = decode_access_token(credentials.credentials)
        if username:
            return username

    # 2. X-Admin-Key (injected by Next.js proxy for backward compat)
    if x_admin_key and x_admin_key == settings.admin_password:
        return "admin"

    raise HTTPException(status_code=401, detail="Invalid or missing credentials")


# Dependency alias
RequireAdmin = Depends(get_admin_key)


# ── Admin user bootstrap ──────────────────────────────────────────────────────

async def ensure_admin_user() -> None:
    """Create default admin/admin user if no users exist in the database."""
    from sqlalchemy import select
    from app.database import async_session
    from app.models.user import User

    async with async_session() as session:
        result = await session.execute(select(User).limit(1))
        if result.scalars().first() is None:
            admin = User(
                username="admin",
                password_hash=hash_password("admin"),
                display_name="Administrator",
                is_active=True,
            )
            session.add(admin)
            await session.commit()
