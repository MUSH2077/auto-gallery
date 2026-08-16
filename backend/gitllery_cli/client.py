from __future__ import annotations

import getpass
import os
import sys
from typing import Any

import httpx


class APIError(RuntimeError):
    def __init__(self, status_code: int, detail: Any):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class GitlleryClient:
    def __init__(self, base_url: str, token: str | None, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}))
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                timeout=self.timeout,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise APIError(503, str(exc)) from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.json())
            except ValueError:
                detail = response.text
            raise APIError(response.status_code, detail)
        if response.status_code == 204:
            return None
        return response.json()


def token_from_inputs(profile: dict[str, Any], *, token_stdin: bool) -> str | None:
    if token_stdin:
        return sys.stdin.readline().strip()
    return os.environ.get("GITLLERY_TOKEN") or profile.get("token")


def login(base_url: str, username: str, timeout: float) -> str:
    password = getpass.getpass("Password: ")
    client = GitlleryClient(base_url, None, timeout)
    payload = client.request(
        "POST",
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    return str(payload["access_token"])
