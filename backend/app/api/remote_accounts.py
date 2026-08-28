"""Authenticated user's remote discovery accounts and X OAuth setup."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequirePermission
from app.config import settings
from app.database import get_db
from app.schemas.remote_discovery import RemoteAccountCreate, RemoteAccountRead, RemoteAccountUpdate
from app.services.redis_client import get_redis
from app.services.remote_accounts import RemoteAccountService
from app.services.x_oauth import XOAuthPKCEState, get_x_oauth_exchange, validate_x_oauth_scopes


router = APIRouter(dependencies=[RequirePermission("subscriptions")])


def _not_found_or_bad_request(exc: ValueError) -> HTTPException:
    status = 404 if "not found" in str(exc).casefold() else 400
    return HTTPException(status_code=status, detail=str(exc))


@router.get("", response_model=list[RemoteAccountRead])
async def list_remote_accounts(
    offset: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    return await RemoteAccountService(db, user.id).list(offset=offset, limit=limit)


@router.post("", response_model=RemoteAccountRead, status_code=201)
async def create_remote_account(
    data: RemoteAccountCreate,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    try:
        result = await RemoteAccountService(db, user.id).create(data.model_dump())
        await db.commit()
        return result
    except (ValueError, RuntimeError) as exc:
        raise _not_found_or_bad_request(exc) from exc


@router.get("/x/oauth/authorize")
async def authorize_x_oauth(
    account_id: UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
    redis=Depends(get_redis),
):
    try:
        if account_id is not None:
            account = await RemoteAccountService(db, user.id).get(account_id)
            if account.source != "x":
                raise ValueError("OAuth reauthentication requires an X remote account")
        result = XOAuthPKCEState(
            redis,
            client_id=settings.x_oauth_client_id,
            redirect_uri=settings.x_oauth_redirect_uri,
        ).authorize(user_id=user.id, account_id=str(account_id) if account_id else None)
    except ValueError as exc:
        status = 404 if "not found" in str(exc).casefold() else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"authorization_url": result.url, "state": result.state, "expires_in": 600}


@router.get("/x/oauth/callback", response_model=RemoteAccountRead)
async def x_oauth_callback(
    state: str = Query(min_length=20, max_length=200),
    code: str = Query(min_length=1, max_length=2000),
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
    redis=Depends(get_redis),
    exchange=Depends(get_x_oauth_exchange),
):
    try:
        payload = XOAuthPKCEState(
            redis,
            client_id=settings.x_oauth_client_id,
            redirect_uri=settings.x_oauth_redirect_uri,
        ).consume(state=state, user_id=user.id)
        token_response = await exchange.exchange(
            code=code,
            verifier=payload.verifier,
            redirect_uri=settings.x_oauth_redirect_uri,
            client_id=settings.x_oauth_client_id,
        )
        credentials = {
            key: str(token_response[key])
            for key in ("access_token", "refresh_token")
            if token_response.get(key)
        }
        credentials["client_id"] = settings.x_oauth_client_id
        scopes = validate_x_oauth_scopes(str(token_response.get("scope") or ""))
        service = RemoteAccountService(db, user.id)
        if payload.account_id:
            result = await service.update(
                UUID(payload.account_id),
                {"auth_method": "oauth2", "credentials": credentials, "scopes": scopes},
            )
        else:
            existing = next(
                (account for account in await service.list(limit=10) if account.source == "x"),
                None,
            )
            if existing:
                result = await service.update(
                    existing.id,
                    {"auth_method": "oauth2", "credentials": credentials, "scopes": scopes},
                )
            else:
                result = await service.create(
                    {
                        "source": "x",
                        "auth_method": "oauth2",
                        "credentials": credentials,
                        "scopes": scopes,
                    }
                )
        await db.commit()
        return result
    except (ValueError, RuntimeError) as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{account_id}", response_model=RemoteAccountRead)
async def get_remote_account(
    account_id: UUID,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    try:
        return await RemoteAccountService(db, user.id).get(account_id)
    except (ValueError, RuntimeError) as exc:
        raise _not_found_or_bad_request(exc) from exc


@router.patch("/{account_id}", response_model=RemoteAccountRead)
async def update_remote_account(
    account_id: UUID,
    data: RemoteAccountUpdate,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    try:
        result = await RemoteAccountService(db, user.id).update(
            account_id, data.model_dump(exclude_unset=True)
        )
        await db.commit()
        return result
    except (ValueError, RuntimeError) as exc:
        raise _not_found_or_bad_request(exc) from exc


@router.delete("/{account_id}", status_code=204)
async def delete_remote_account(
    account_id: UUID,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    try:
        await RemoteAccountService(db, user.id).delete(account_id)
        await db.commit()
    except (ValueError, RuntimeError) as exc:
        raise _not_found_or_bad_request(exc) from exc


@router.post("/{account_id}/test", response_model=RemoteAccountRead)
async def test_remote_account(
    account_id: UUID,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    try:
        result = await RemoteAccountService(db, user.id).test(account_id)
        await db.commit()
        return result
    except ValueError as exc:
        await db.commit()
        raise _not_found_or_bad_request(exc) from exc
    except Exception as exc:
        await db.commit()
        raise HTTPException(status_code=502, detail="Remote account validation failed") from exc


@router.get("/{account_id}/collections")
async def list_remote_account_collections(
    account_id: UUID,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    try:
        collections = await RemoteAccountService(db, user.id).collections(account_id)
        return [
            {"id": item.id, "name": item.name, "selector": dict(item.selector)}
            for item in collections
        ]
    except ValueError as exc:
        raise _not_found_or_bad_request(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Remote collections request failed") from exc
