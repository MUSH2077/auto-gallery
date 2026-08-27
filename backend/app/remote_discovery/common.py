"""Injected HTTP boundary and stable error vocabulary for discovery adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


class RemoteDiscoveryError(RuntimeError):
    pass


class RemoteReauthenticationRequired(RemoteDiscoveryError):
    def __init__(self, status_code: int, message: str = "remote account requires reauthentication"):
        self.status_code = status_code
        super().__init__(message)


class RemoteRateLimited(RemoteDiscoveryError):
    def __init__(self, retry_after_seconds: int | None):
        self.retry_after_seconds = retry_after_seconds
        detail = f"; retry after {retry_after_seconds}s" if retry_after_seconds is not None else ""
        super().__init__(f"remote provider rate limited discovery{detail}")


class MalformedRemoteResponse(RemoteDiscoveryError):
    pass


@dataclass(frozen=True)
class RemoteHTTPResponse:
    status_code: int
    payload: Any
    headers: Mapping[str, str] = field(default_factory=dict)


class RemoteHTTPTransport(Protocol):
    async def request(self, method: str, url: str, **kwargs: Any) -> RemoteHTTPResponse: ...


class HttpxRemoteTransport:
    """Default runtime boundary; tests inject deterministic transports instead."""

    def __init__(self, *, timeout_seconds: float = 20.0):
        self.timeout_seconds = timeout_seconds

    async def request(self, method: str, url: str, **kwargs: Any) -> RemoteHTTPResponse:
        import httpx

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.request(method, url, **kwargs)
        try:
            payload = response.json()
        except ValueError as exc:
            raise MalformedRemoteResponse("remote provider returned non-JSON data") from exc
        return RemoteHTTPResponse(response.status_code, payload, dict(response.headers))


def checked_payload(response: RemoteHTTPResponse, *, provider: str) -> Mapping[str, Any]:
    if response.status_code in {401, 403}:
        raise RemoteReauthenticationRequired(response.status_code)
    if response.status_code == 429:
        raw_retry = response.headers.get("Retry-After") or response.headers.get("retry-after")
        try:
            retry_after = int(raw_retry) if raw_retry is not None else None
        except (TypeError, ValueError):
            retry_after = None
        raise RemoteRateLimited(retry_after)
    if not 200 <= response.status_code < 300:
        raise RemoteDiscoveryError(
            f"{provider} discovery request failed with HTTP {response.status_code}"
        )
    if not isinstance(response.payload, Mapping):
        raise MalformedRemoteResponse(f"{provider} returned a malformed response object")
    return response.payload


def required_text(credentials: Mapping[str, Any], key: str, *, provider: str) -> str:
    value = credentials.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RemoteReauthenticationRequired(401, f"{provider} credential {key!r} is missing")
    return value


def validate_page_size(page_size: int) -> None:
    if not 1 <= page_size <= 1000:
        raise ValueError("discovery page_size must be between 1 and 1000")
