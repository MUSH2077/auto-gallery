import bcrypt as _bcrypt
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from jwt import InvalidTokenError

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


def create_access_token(username: str, must_change_password: bool = False) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode(
        {"sub": username, "exp": expire, "pwd_chg_required": must_change_password},
        settings.secret_key,
        algorithm=ALGORITHM,
    )


def decode_access_token_payload(token: str) -> Optional[dict[str, Any]]:
    """Return token payload, or None if invalid/expired."""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        return payload
    except InvalidTokenError:
        return None


def decode_access_token(token: str) -> Optional[str]:
    """Return username from token, or None if invalid/expired."""
    payload = decode_access_token_payload(token)
    if not payload:
        return None
    return payload.get("sub")


# ── Main auth dependency ──────────────────────────────────────────────────────

async def get_admin_key(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    """Require Bearer JWT token for admin APIs."""
    if credentials and credentials.credentials:
        payload = decode_access_token_payload(credentials.credentials)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid or missing credentials")

        username = payload.get("sub")
        if username:
            if payload.get("pwd_chg_required") and request.url.path not in ("/api/v1/auth/change-password", "/api/v1/auth/me"):
                raise HTTPException(status_code=403, detail="Password change required")
            return username

    raise HTTPException(status_code=401, detail="Invalid or missing credentials")


# Dependency alias
RequireAdmin = Depends(get_admin_key)


# ── Per-user permission dependencies ──────────────────────────────────────────

async def _load_active_user(username: str):
    from sqlalchemy import select
    from app.database import async_session
    from app.models.user import User
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid or missing credentials")
    return user


async def require_docs_admin(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    """Authenticate the human-facing API contract without weakening APIs.

    Swagger/ReDoc can be opened from the signed-in admin browser via the
    existing ``ag_token`` cookie. Programmatic schema clients may instead send
    the same JWT as a Bearer token. Business endpoints continue to accept
    Bearer authentication only, avoiding cookie-authenticated mutations.
    """
    token = credentials.credentials if credentials and credentials.credentials else request.cookies.get("ag_token")
    payload = decode_access_token_payload(token) if token else None
    if not payload or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Administrator authentication required")
    if payload.get("pwd_chg_required"):
        raise HTTPException(status_code=403, detail="Password change required")
    user = await _load_active_user(payload["sub"])
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user


def RequirePermission(module: str):
    async def _check(
        request: Request,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    ):
        username = await get_admin_key(request, credentials)  # existing 401 + pwd-change logic
        user = await _load_active_user(username)
        if user.is_admin or module in (user.permissions or []):
            return user
        raise HTTPException(status_code=403, detail=f"Missing permission: {module}")
    return Depends(_check)


def RequireAnyPermission(*modules: str):
    """Require at least one permission while returning the resolved user.

    Search is the first cross-domain interface that needs this: a library-only
    user may search works while a subscriptions-only user may search
    repositories.  Keeping the check here avoids weakening either domain's
    existing route permissions.
    """

    required = tuple(dict.fromkeys(modules))
    if not required:
        raise ValueError("RequireAnyPermission needs at least one module")

    async def _check(
        request: Request,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    ):
        username = await get_admin_key(request, credentials)
        user = await _load_active_user(username)
        if user.is_admin or any(module in (user.permissions or []) for module in required):
            return user
        raise HTTPException(status_code=403, detail=f"Missing any permission: {', '.join(required)}")

    return Depends(_check)


async def _admin_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    username = await get_admin_key(request, credentials)
    user = await _load_active_user(username)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return user

RequireAdminUser = Depends(_admin_user)


# ── Admin user bootstrap ──────────────────────────────────────────────────────

async def ensure_admin_user() -> None:
    """Create bootstrap admin user if no users exist.

    If an admin already exists but still has must_change_password=False
    (created before the password-rotation feature was deployed), set
    it to True so the admin is prompted to change the default password.
    """
    from sqlalchemy import select, update
    from app.database import async_session
    from app.models.user import User

    async with async_session() as session:
        result = await session.execute(select(User).limit(1))
        if result.scalars().first() is None:
            bootstrap_password = (settings.admin_password or "").strip()
            if not bootstrap_password:
                raise RuntimeError("ADMIN_PASSWORD must be set before bootstrapping the admin user.")
            admin = User(
                username="admin",
                password_hash=hash_password(bootstrap_password),
                display_name="Administrator",
                is_active=True,
                is_admin=True,
                must_change_password=True,
            )
            session.add(admin)
            await session.commit()
        # Once password is changed, never force-reset on restart
