"""Security contract for opaque Pixiv media and remote action tickets."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest


def test_media_ticket_is_opaque_bound_and_expires():
    from app.services.remote_access_tokens import RemoteAccessTokenError, RemoteAccessTokenService

    now = [1_800_000_000]
    service = RemoteAccessTokenService(
        secret="remote-ticket-test-secret-with-32-bytes",
        clock=lambda: now[0],
    )
    candidate_id = uuid4()
    account_id = uuid4()
    upstream = "https://i.pximg.net/c/540x540_70/img-master/example.jpg"

    token = service.issue_media(
        user_id=41,
        candidate_id=candidate_id,
        remote_account_id=account_id,
        credential_generation=7,
        upstream_url=upstream,
        variant="thumbnail",
    )

    assert upstream not in token
    assert service.verify_media(token) == {
        "user_id": 41,
        "candidate_id": str(candidate_id),
        "remote_account_id": str(account_id),
        "credential_generation": 7,
        "upstream_url": upstream,
        "variant": "thumbnail",
    }

    with pytest.raises(RemoteAccessTokenError, match="invalid"):
        service.verify_media(f"{token[:-1]}x")

    now[0] += 601
    with pytest.raises(RemoteAccessTokenError, match="expired"):
        service.verify_media(token)


@pytest.mark.parametrize(
    "url",
    [
        "http://i.pximg.net/example.jpg",
        "https://evil.example/example.jpg",
        "https://i.pximg.net.evil.example/example.jpg",
        "https://user@i.pximg.net/example.jpg",
        "https://i.pximg.net:444/example.jpg",
    ],
)
def test_media_ticket_rejects_untrusted_upstream_urls(url):
    from app.services.remote_access_tokens import RemoteAccessTokenService

    service = RemoteAccessTokenService(secret="remote-ticket-test-secret-with-32-bytes")
    with pytest.raises(ValueError, match="Pixiv media URL"):
        service.issue_media(
            user_id=1,
            candidate_id=uuid4(),
            remote_account_id=uuid4(),
            credential_generation=1,
            upstream_url=url,
            variant="avatar",
        )


@pytest.mark.asyncio
async def test_pixiv_media_fetch_disables_redirects_and_sets_referer():
    from app.services.remote_media import RemoteMediaError, fetch_pixiv_media

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg"},
            content=b"jpeg-bytes",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_pixiv_media(
            "https://i.pximg.net/example.jpg",
            variant="avatar",
            client=client,
        )

    assert result.content == b"jpeg-bytes"
    assert result.content_type == "image/jpeg"
    assert requests[0].headers["Referer"] == "https://www.pixiv.net/"

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/image.jpg"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect_handler)) as client:
        with pytest.raises(RemoteMediaError, match="redirect"):
            await fetch_pixiv_media(
                "https://i.pximg.net/example.jpg",
                variant="avatar",
                client=client,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variant", "content_type", "body", "message"),
    [
        ("thumbnail", "text/html", b"not an image", "image MIME"),
        ("avatar", "image/jpeg", b"x" * (5 * 1024 * 1024 + 1), "size limit"),
        ("preview", "image/png", b"x" * (25 * 1024 * 1024 + 1), "size limit"),
    ],
    ids=["wrong-mime", "avatar-too-large", "preview-too-large"],
)
async def test_pixiv_media_fetch_rejects_wrong_mime_and_oversize(
    variant, content_type, body, message
):
    from app.services.remote_media import RemoteMediaError, fetch_pixiv_media

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": content_type},
            content=body,
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RemoteMediaError, match=message):
            await fetch_pixiv_media(
                "https://i.pximg.net/example.jpg",
                variant=variant,
                client=client,
            )
