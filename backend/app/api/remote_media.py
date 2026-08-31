"""Signed, origin-restricted remote image proxy."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import DiscoveryCandidate, RemoteAccount
from app.services.remote_access_tokens import RemoteAccessTokenError, RemoteAccessTokenService
from app.services.remote_discovery_rollout import RemoteDiscoveryUnavailable, require_preview
from app.services.remote_media import RemoteMediaError, fetch_pixiv_media


router = APIRouter()


@router.get("/{token}", responses={200: {"content": {"image/*": {}}}})
async def get_remote_media(token: str, db: AsyncSession = Depends(get_db)):
    tickets = RemoteAccessTokenService()
    try:
        payload = tickets.verify_media(token)
        candidate_id = UUID(str(payload["candidate_id"]))
        account_id = UUID(str(payload["remote_account_id"]))
        row = (
            await db.execute(
                select(DiscoveryCandidate, RemoteAccount)
                .join(RemoteAccount, RemoteAccount.id == DiscoveryCandidate.remote_account_id)
                .where(
                    DiscoveryCandidate.id == candidate_id,
                    DiscoveryCandidate.user_id == int(payload["user_id"]),
                    DiscoveryCandidate.remote_account_id == account_id,
                    RemoteAccount.user_id == int(payload["user_id"]),
                    RemoteAccount.credential_generation
                    == int(payload["credential_generation"]),
                    RemoteAccount.is_enabled.is_(True),
                )
            )
        ).first()
        if row is None:
            raise RemoteAccessTokenError("remote access token is no longer valid")
        _candidate, account = row
        require_preview(account.source)
        if account.source != "pixiv":
            raise RemoteAccessTokenError("remote access token is invalid")
        result = await fetch_pixiv_media(
            str(payload["upstream_url"]),
            variant=payload["variant"],
        )
    except RemoteDiscoveryUnavailable as exc:
        raise HTTPException(status_code=404, detail={"code": "remote_media_unavailable"}) from exc
    except RemoteAccessTokenError as exc:
        raise HTTPException(status_code=404, detail={"code": "invalid_remote_media_token"}) from exc
    except RemoteMediaError as exc:
        raise HTTPException(status_code=502, detail={"code": "remote_media_failed"}) from exc
    return Response(
        content=result.content,
        media_type=result.content_type,
        headers={
            "Cache-Control": "private, max-age=600",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
