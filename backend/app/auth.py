import bcrypt as _bcrypt
from datetime import datetime, timedelta, timezone
import os
import secrets
from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.config import settings

# ── Password hashing ──────────────────────────────────────────────────────────
ALGORITHM = "HS256"

bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_access_token(username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
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
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    """Require Bearer JWT token for admin APIs."""
    if credentials and credentials.credentials:
        username = decode_access_token(credentials.credentials)
        if username:
            return username

    raise HTTPException(status_code=401, detail="Invalid or missing credentials")


# Dependency alias
RequireAdmin = Depends(get_admin_key)


# ── Admin user bootstrap ──────────────────────────────────────────────────────

async def ensure_admin_user() -> None:
    """Create bootstrap admin user if no users exist in the database."""
    from sqlalchemy import select
    from app.database import async_session
    from app.models.user import User

    async with async_session() as session:
        result = await session.execute(select(User).limit(1))
        if result.scalars().first() is None:
            bootstrap_password = (settings.admin_password or "").strip()
            if not bootstrap_password or bootstrap_password == "changeme":
                bootstrap_password = secrets.token_urlsafe(18)
                pw_dir = settings.app_config_root
                os.makedirs(pw_dir, exist_ok=True)
                pw_file = os.path.join(pw_dir, "bootstrap_admin_password")
                with open(pw_file, "w", encoding="utf-8") as f:
                    f.write(bootstrap_password)
                # Use stdlib logger to avoid importing app.main logger here.
                import logging
                logging.getLogger(__name__).warning(
                    "ADMIN_PASSWORD is unset/weak; generated bootstrap admin password at %s",
                    pw_file,
                )
            admin = User(
                username="admin",
                password_hash=hash_password(bootstrap_password),
                display_name="Administrator",
                is_active=True,
            )
            session.add(admin)
            await session.commit()
