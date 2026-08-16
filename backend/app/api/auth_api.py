"""Authentication endpoints: login, me, change-password."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    bearer_scheme,
    create_access_token,
    decode_access_token,
    get_admin_key,
    hash_password,
    verify_password,
)
from app.database import get_db
from app.models.user import User
from app.permissions import PERMISSION_MODULES
from app.schemas.users import MeOut, PreferencesIn, PreferencesOut
from app.services.rate_limiter import RateLimiter
from app.services.ws_tickets import issue_ws_ticket

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
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_db),
) -> User:
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    username = decode_access_token(credentials.credentials)
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    result = await session.execute(select(User).where(User.username == username, User.is_active == True))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, session: AsyncSession = Depends(get_db)):
    # Rate limit by client IP (fail open if Redis is unavailable)
    client_ip = request.client.host if request.client else "unknown"
    allowed, remaining, retry_after = await _login_limiter.check(client_ip)
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

    must_change_password = bool(user.must_change_password)
    token = create_access_token(user.username, must_change_password=must_change_password)

    user.last_login_at = func.now()
    await session.commit()

    return TokenResponse(access_token=token, must_change_password=must_change_password)


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
async def create_websocket_ticket(username: str = Depends(get_admin_key)):
    ticket, ttl = issue_ws_ticket(username)
    return WebSocketTicketResponse(ticket=ticket, expires_in=ttl)


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    if not verify_password(body.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    if len(body.new_password) < 6:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New password must be at least 6 characters")
    try:
        new_password_hash = hash_password(body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # Re-fetch in this session to avoid detached instance
    result = await session.execute(select(User).where(User.id == current_user.id))
    user = result.scalars().first()
    user.password_hash = new_password_hash
    user.must_change_password = False
    user.updated_at = datetime.now(timezone.utc)
    await session.commit()
    token = create_access_token(user.username, must_change_password=False)

    return {"ok": True, "access_token": token, "token_type": "bearer", "must_change_password": False}
