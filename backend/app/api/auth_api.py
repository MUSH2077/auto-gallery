"""Authentication endpoints: login, me, change-password."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    create_access_token,
    get_admin_key,
    hash_password,
    verify_password,
)
from app.database import get_db
from app.models.user import User
from app.permissions import PERMISSION_MODULES
from app.schemas.users import MeOut, PreferencesIn, PreferencesOut
from app.services.rate_limiter import RateLimiter
from app.services.browser_sessions import (
    CSRF_COOKIE, SESSION_COOKIE, SessionStoreUnavailable, browser_origin,
    create_browser_session, load_browser_session, revoke_browser_session,
    session_lifetime_seconds, verify_browser_csrf,
)
from app.services.ws_tickets import issue_ws_ticket
from redis.exceptions import RedisError

_ALLOWED_PREFERENCE_KEYS = {"theme", "palette", "lang", "appearance", "slideshow"}

router = APIRouter()

# Rate limit login attempts: 5 per minute per IP
_login_limiter = RateLimiter(prefix="login", max_requests=5, window_seconds=60)


# ── Schemas ───────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    must_change_password: bool = False


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class WebSocketTicketResponse(BaseModel):
    ticket: str
    expires_in: int


# ── Helper ────────────────────────────────────────────────────────────────────

async def get_current_user(
    request: Request,
    username: str = Depends(get_admin_key),
    session: AsyncSession = Depends(get_db),
) -> User:
    cached = getattr(request.state, "auth_user", None)
    if cached is not None:
        return cached
    result = await session.execute(select(User).where(User.username == username, User.is_active == True))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user


async def _authenticate_login(body: LoginRequest, request: Request, session: AsyncSession) -> User:
    # Bearer and browser login share the same IP limiter.
    client_ip = request.client.host if request.client else "unknown"
    allowed, _, retry_after = await _login_limiter.check(client_ip)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    result = await session.execute(select(User).where(User.username == body.username, User.is_active == True))
    user = result.scalars().first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect username or password")
    user.last_login_at = func.now()
    await session.commit()
    return user


def _set_browser_cookies(response: Response, token: str, csrf: str, origin: str) -> None:
    secure = origin.startswith("https://")
    max_age = session_lifetime_seconds()
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=secure, samesite="lax", path="/", max_age=max_age)
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, secure=secure, samesite="lax", path="/", max_age=max_age)
    response.delete_cookie("ag_token", path="/")


def _clear_browser_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    response.delete_cookie("ag_token", path="/")


def _session_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="Browser session service unavailable")


async def _change_password(body: ChangePasswordRequest, current_user: User, session: AsyncSession) -> User:
    if not verify_password(body.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    if len(body.new_password) < 6:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New password must be at least 6 characters")
    try:
        new_password_hash = hash_password(body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    result = await session.execute(select(User).where(User.id == current_user.id))
    user = result.scalars().first()
    user.password_hash = new_password_hash
    user.must_change_password = False
    user.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return user


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, session: AsyncSession = Depends(get_db)):
    user = await _authenticate_login(body, request, session)
    must_change_password = bool(user.must_change_password)
    return TokenResponse(
        access_token=create_access_token(user.username, must_change_password=must_change_password),
        must_change_password=must_change_password,
    )


@router.post("/browser/login")
async def browser_login(
    body: LoginRequest, request: Request, response: Response,
    session: AsyncSession = Depends(get_db),
):
    origin = browser_origin(request)
    user = await _authenticate_login(body, request, session)
    try:
        token, browser_session = await create_browser_session(user)
    except SessionStoreUnavailable as exc:
        raise _session_unavailable() from exc
    _set_browser_cookies(response, token, browser_session.csrf, origin)
    return {"ok": True, "must_change_password": bool(user.must_change_password)}


@router.post("/browser/logout")
async def browser_logout(request: Request, response: Response):
    browser_origin(request)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        try:
            browser_session = await load_browser_session(token)
            if browser_session is not None:
                verify_browser_csrf(request, browser_session)
                await revoke_browser_session(token)
        except SessionStoreUnavailable as exc:
            raise _session_unavailable() from exc
    _clear_browser_cookies(response)
    return {"ok": True}


@router.get("/me", response_model=MeOut)
async def me(current_user: User = Depends(get_current_user)):
    return MeOut(
        id=current_user.id,
        username=current_user.username,
        display_name=current_user.display_name,
        is_admin=current_user.is_admin,
        is_active=current_user.is_active,
        permissions=current_user.permissions or [],
        modules=PERMISSION_MODULES,
        preferences=current_user.preferences or {},
        nsfw_visible=current_user.nsfw_visible,
        upload_quota_bytes=current_user.upload_quota_bytes,
        upload_used_bytes=current_user.upload_used_bytes,
        must_change_password=bool(current_user.must_change_password),
    )


@router.put("/me/preferences", response_model=PreferencesOut)
async def update_my_preferences(
    body: PreferencesIn,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    invalid = set(body.preferences) - _ALLOWED_PREFERENCE_KEYS
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid preference key(s): {', '.join(sorted(invalid))}",
        )

    # Re-fetch in this session to avoid detached instance
    result = await session.execute(select(User).where(User.id == current_user.id))
    user = result.scalars().first()
    user.preferences = dict(body.preferences)
    await session.commit()
    return {"preferences": user.preferences}


@router.post("/ws-ticket", response_model=WebSocketTicketResponse)
async def create_websocket_ticket(request: Request, username: str = Depends(get_admin_key)):
    try:
        ticket, ttl = issue_ws_ticket(username, getattr(request.state, "browser_session_token", None))
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Ticket service unavailable") from exc
    return WebSocketTicketResponse(ticket=ticket, expires_in=ttl)


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    if getattr(request.state, "browser_session", None) is not None:
        raise HTTPException(status_code=401, detail="Bearer credentials required")
    user = await _change_password(body, current_user, session)
    token = create_access_token(user.username, must_change_password=False)
    return {"ok": True, "access_token": token, "token_type": "bearer", "must_change_password": False}


@router.post("/browser/change-password")
async def browser_change_password(
    body: ChangePasswordRequest, request: Request, response: Response,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    browser_origin(request)
    old_token = request.cookies.get(SESSION_COOKIE)
    if not old_token or not getattr(request.state, "browser_session", None):
        raise HTTPException(status_code=401, detail="Browser session required")
    user = await _change_password(body, current_user, session)
    try:
        token, browser_session = await create_browser_session(user)
        await revoke_browser_session(old_token)
    except SessionStoreUnavailable as exc:
        raise _session_unavailable() from exc
    _set_browser_cookies(response, token, browser_session.csrf, request.headers["origin"])
    return {"ok": True, "must_change_password": False}
