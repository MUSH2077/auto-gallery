import bcrypt as _bcrypt
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, TYPE_CHECKING

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from jwt import InvalidTokenError

from app.config import settings
from app.services.browser_sessions import (
    SESSION_COOKIE, SessionStoreUnavailable, browser_origin, load_browser_session,
    password_fingerprint, verify_browser_csrf,
)

# ── Password hashing ──────────────────────────────────────────────────────────
ALGORITHM = "HS256"
BCRYPT_MAX_PASSWORD_BYTES = 72

bearer_scheme = HTTPBearer(auto_error=False)
_password_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="password")

if TYPE_CHECKING:
    from app.models.user import User


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError(
            f"password must be at most {BCRYPT_MAX_PASSWORD_BYTES} bytes when UTF-8 encoded"
        )
    return _bcrypt.hashpw(encoded, _bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


async def async_hash_password(password: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(_password_workers, hash_password, password)


async def async_verify_password(plain: str, hashed: str) -> bool:
    return await asyncio.get_running_loop().run_in_executor(_password_workers, verify_password, plain, hashed)


def create_access_token(user: "User") -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode(
        {"sub": user.username, "uid": user.id, "cv": user.credential_version, "exp": expire},
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

async def get_authenticated_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    """Resolve an explicit Bearer token or a revocable browser session."""
    if credentials and credentials.credentials:
        payload = decode_access_token_payload(credentials.credentials)
        if not payload or not payload.get("sub"):
            raise HTTPException(status_code=401, detail="Invalid or missing credentials")
        user_id, version = payload.get("uid"), payload.get("cv")
        # Tokens issued before identity/version binding require a new login.
        if type(user_id) is not int or user_id <= 0 or type(version) is not int or version <= 0:
            raise HTTPException(status_code=401, detail="Invalid or missing credentials")
        user = await _load_active_user_by_id(user_id)
        if user.credential_version != version:
            raise HTTPException(status_code=401, detail="Invalid or missing credentials")
        if user.must_change_password and request.url.path not in (
            "/api/v1/auth/change-password", "/api/v1/auth/me"
        ):
            raise HTTPException(status_code=403, detail="Password change required")
        request.state.auth_user = user
        return user

    if request.headers.get("authorization"):
        raise HTTPException(status_code=401, detail="Invalid or missing credentials")
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Invalid or missing credentials")
    try:
        browser_session = await load_browser_session(token)
    except SessionStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail="Browser session service unavailable") from exc
    if browser_session is None:
        raise HTTPException(status_code=401, detail="Invalid or missing credentials")
    user = await _load_active_user_by_id(browser_session.user_id)
    if password_fingerprint(user.password_hash) != browser_session.password_fingerprint:
        raise HTTPException(status_code=401, detail="Invalid or missing credentials")
    if user.must_change_password and request.url.path not in (
        "/api/v1/auth/browser/change-password", "/api/v1/auth/me"
    ):
        raise HTTPException(status_code=403, detail="Password change required")
    # Credentialed cross-origin reads must use the browser-session allowlist,
    # even when a separate API client is present in CORS_ORIGINS.
    if request.headers.get("origin") or request.method not in {"GET", "HEAD", "OPTIONS"}:
        browser_origin(request)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        verify_browser_csrf(request, browser_session)
    request.state.auth_user = user
    request.state.browser_session = browser_session
    request.state.browser_session_token = token
    return user


async def get_admin_key(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    """Username adapter for existing operation actor labels."""
    return (await get_authenticated_user(request, credentials)).username


# Dependency alias
RequireAdmin = Depends(get_admin_key)


# ── Per-user permission dependencies ──────────────────────────────────────────

async def _load_active_user_by_id(user_id: int):
    from sqlalchemy import select
    from app.database import async_session
    from app.models.user import User
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid or missing credentials")
    return user


async def require_docs_admin(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    """Authenticate API documentation using the shared auth resolver."""
    user = await get_authenticated_user(request, credentials)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user


def RequirePermission(module: str):
    async def _check(
        request: Request,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    ):
        user = await get_authenticated_user(request, credentials)
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
        user = await get_authenticated_user(request, credentials)
        if user.is_admin or any(module in (user.permissions or []) for module in required):
            return user
        raise HTTPException(status_code=403, detail=f"Missing any permission: {', '.join(required)}")

    return Depends(_check)


async def _admin_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    user = await get_authenticated_user(request, credentials)
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
                password_hash=await async_hash_password(bootstrap_password),
                display_name="Administrator",
                is_active=True,
                is_admin=True,
                must_change_password=True,
            )
            session.add(admin)
            await session.commit()
        # Once password is changed, never force-reset on restart
