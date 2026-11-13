"""Configured proxy and provider connectivity probes, without HTTP adapters."""
import asyncio
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.settings import get_system_setting

logger = logging.getLogger(__name__)
DEFAULT_PROXY = {"http_proxy": "", "https_proxy": "", "no_proxy": "localhost,127.0.0.1,::1", "enabled": False}

async def probe_proxy_connectivity(db: AsyncSession):
    """Test connectivity through the configured proxy to key external sites."""
    import urllib.error
    import urllib.parse
    import urllib.request
    import ssl
    import time

    config = {**DEFAULT_PROXY, **await get_system_setting(db, "proxy")}
    enabled = config.get("enabled", False)
    ssl_verify = config.get("ssl_verify", True)

    # Build SSL context — skip verification if user disabled it (MITM proxy)
    ssl_ctx = ssl.create_default_context()
    if not ssl_verify:
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    # Build proxy opener
    opener = None
    if enabled:
        proxies = {}
        http_proxy = config.get("http_proxy", "")
        https_proxy = config.get("https_proxy", "")
        if http_proxy:
            proxies["http"] = http_proxy
        if https_proxy:
            proxies["https"] = https_proxy
        if proxies:
            proxy_handler = urllib.request.ProxyHandler(proxies)
            opener = urllib.request.build_opener(proxy_handler, urllib.request.HTTPSHandler(context=ssl_ctx))

    if opener is None:
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl_ctx))

    # ── Proxy reachability check ──
    proxy_reachable = None
    proxy_reachable_error = ""
    if enabled:
        proxy_url = config.get("http_proxy") or config.get("https_proxy", "")
        if proxy_url:
            import socket
            parsed_proxy = urllib.parse.urlsplit(proxy_url)
            if parsed_proxy.hostname:
                host = parsed_proxy.hostname
                port = parsed_proxy.port or 7890
                try:
                    sock = socket.create_connection((host, port), timeout=5)
                    sock.close()
                    proxy_reachable = True
                except Exception:
                    logger.warning(
                        "Configured proxy endpoint is unreachable",
                        exc_info=True,
                    )
                    proxy_reachable = False
                    proxy_reachable_error = "Cannot connect to the configured proxy endpoint."

    direct_opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl_ctx))

    targets = [
        {"name": "Pixiv", "url": "https://www.pixiv.net"},
        {"name": "Danbooru", "url": "https://danbooru.donmai.us"},
        {"name": "Danbooru API", "url": "https://danbooru.donmai.us/artists.json?limit=1"},
        {"name": "Iwara", "url": "https://www.iwara.tv"},
        {"name": "Twitter/X", "url": "https://x.com"},
        {"name": "Pinterest", "url": "https://www.pinterest.com"},
        {"name": "LOFTER", "url": "https://www.lofter.com"},
        {"name": "GitHub", "url": "https://github.com"},
        {"name": "Google", "url": "https://www.google.com"},
    ]

    import concurrent.futures
    TEST_TIMEOUT = 3  # per-test timeout, all 9 run concurrently with 9 workers

    def _test_one(t):
        name, url = t["name"], t["url"]
        # Direct
        d_ok, d_ms, d_err = False, 0, ""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "auto-gallery/0.1"})
            start = time.monotonic()
            with direct_opener.open(req, timeout=TEST_TIMEOUT) as resp:
                d_ok = resp.status < 500
            d_ms = int((time.monotonic() - start) * 1000)
        except urllib.error.HTTPError as e:
            d_err, d_ok = f"HTTP {e.code}", e.code < 500
        except urllib.error.URLError as exc:
            d_err = (
                "Connection timed out"
                if isinstance(exc.reason, TimeoutError)
                else f"Connection failed ({type(exc.reason).__name__})"
            )
        except Exception:
            logger.warning("Direct connectivity probe failed for %s", name, exc_info=True)
            d_err = "Connection test failed"

        # Proxy
        p_ok, p_ms, p_err = False, 0, ""
        if enabled:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "auto-gallery/0.1"})
                start = time.monotonic()
                with opener.open(req, timeout=TEST_TIMEOUT) as resp:
                    p_ok = resp.status < 500
                p_ms = int((time.monotonic() - start) * 1000)
            except urllib.error.HTTPError as e:
                p_err, p_ok = f"HTTP {e.code}", e.code < 500
            except urllib.error.URLError as exc:
                p_err = (
                    "Connection timed out"
                    if isinstance(exc.reason, TimeoutError)
                    else f"Connection failed ({type(exc.reason).__name__})"
                )
            except Exception:
                logger.warning("Proxy connectivity probe failed for %s", name, exc_info=True)
                p_err = "Connection test failed"

        return {"name": name, "url": url,
                "direct_ok": d_ok, "direct_ms": d_ms, "direct_error": d_err,
                "proxy_ok": p_ok if enabled else None, "proxy_ms": p_ms if enabled else None,
                "proxy_error": p_err if enabled else ""}

    import concurrent.futures
    logger.info("Proxy test starting: enabled=%s proxy=%s targets=%d", enabled,
                redact_proxy_url(config.get("http_proxy")), len(targets))

    def _run_all():
        # list(executor.map(...)) blocks until all probes finish (~TEST_TIMEOUT)
        # — offloaded so the event loop isn't held for the duration.
        with concurrent.futures.ThreadPoolExecutor(max_workers=9) as executor:
            return list(executor.map(_test_one, targets))
    results = await asyncio.to_thread(_run_all)
    ok = sum(1 for r in results if r["proxy_ok"])
    fail = sum(1 for r in results if r["proxy_ok"] is False)
    logger.info("Proxy test complete: %d OK, %d FAIL, %d total (reachable=%s)",
                ok, fail, len(results), proxy_reachable)

    return {
        "proxy_enabled": enabled,
        "proxy_reachable": proxy_reachable,
        "proxy_reachable_error": proxy_reachable_error,
        "proxy_config": {
            "http": redact_proxy_url(config.get("http_proxy")),
            "https": redact_proxy_url(config.get("https_proxy")),
        },
        "results": results,
    }


def redact_proxy_url(value: object) -> str:
    """Return a useful proxy endpoint without persisting or logging userinfo."""

    import urllib.parse

    raw = str(value or "").strip()
    if not raw:
        return "not set"
    try:
        parsed = urllib.parse.urlsplit(raw)
        hostname = parsed.hostname
        if not hostname:
            return "configured"
        rendered_host = f"[{hostname}]" if ":" in hostname else hostname
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urllib.parse.urlunsplit(
            (parsed.scheme, f"{rendered_host}{port}", parsed.path, parsed.query, parsed.fragment)
        )
    except (TypeError, ValueError):
        return "configured"


TEST_URLS = {
    "pixiv": "https://www.pixiv.net/users/11",
    "twitter": "https://x.com/X",
    "iwara": "https://www.iwara.tv/videos",
    "danbooru": "https://danbooru.donmai.us/posts?tags=rating:s&limit=1",
    "pinterest": "https://www.pinterest.com/pinterest/",
    "lofter": "https://www.lofter.com/tag/%E6%8F%92%E7%94%BB",
    "weibo": "https://weibo.com/u/2803301701",
    "bilibili": "https://space.bilibili.com/8047632",
}


AUTH_ERROR_PATTERNS = [
    (r"(?i)AuthRequired", "Authentication failed — invalid or missing credentials"),
    (r"(?i)AuthorizationError", "Authorization denied — check account permissions"),
    (r"(?i)HttpError.*401", "HTTP 401 Unauthorized — check username/password"),
    (r"(?i)HttpError.*403", "HTTP 403 Forbidden — check account access"),
    (r"(?i)LoginRequired", "Login required — credentials not provided or invalid"),
    (r"(?i)NotFoundError", "User/page not found — check the test URL"),
    (r"(?i)cookie.*(?:expired|invalid|missing)", "Cookie expired or invalid"),
    (r"(?i)token.*(?:expired|invalid|revoked)", "Token expired or revoked"),
    (r"(?i)Could not authenticate", "Authentication rejected by server"),
]


async def probe_source_connection(source: str):
    """Execute gallery-dl connectivity using the saved source configuration."""

    test_url = TEST_URLS[source]
    import json as _json, os, re as _re, subprocess, tempfile, shutil

    tmpdir = tempfile.mkdtemp(prefix="gallerydl-test-")
    tmp_config = os.path.join(tmpdir, "test-config.json")
    try:
        from app.providers import registry as _registry
        from app.services.settings import (
            build_effective_gallerydl_config,
            source_key_for_extractor,
        )

        provider_source = source_key_for_extractor(source)
        provider_config = {}
        try:
            provider = _registry.get(provider_source)
            provider_config = provider.build_gallerydl_config(None)
        except KeyError:
            provider_config = {}
        config = build_effective_gallerydl_config(provider_source, provider_config)
        with open(tmp_config, "w") as f:
            _json.dump(config, f)

        cmd = [
            "gallery-dl", "--config", tmp_config,
            "--write-metadata",
            "--range", "1-1",
            "--destination", tmpdir,
            test_url,
        ]

        env = os.environ.copy()
        proxy_enabled = False
        try:
            from app.services.proxy import _load_proxy_config, get_proxy_env
            proxy_config = await _load_proxy_config()
            proxy_enabled = bool(proxy_config.get("enabled", False))
            env.update(get_proxy_env(proxy_config))
        except Exception:
            logger.warning("Failed to apply proxy env for gallery-dl test", exc_info=True)

        logger.info("Testing %s connectivity: %s (proxy=%s)", source, " ".join(cmd), proxy_enabled)
        # gallery-dl can run up to 120s — off the event loop so a connection
        # test doesn't freeze the whole backend for other users.
        result = await asyncio.to_thread(
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env))
        stderr = result.stderr or ""
        stdout = result.stdout or ""
        combined = stdout + stderr

        auth_issue = None
        for pattern, label in AUTH_ERROR_PATTERNS:
            if _re.search(pattern, combined):
                auth_issue = label
                break

        if result.returncode == 0 and not auth_issue:
            return {
                "source": source, "success": True,
                "message": f"Connection test passed for {source}. Credentials are valid.",
                "details": stderr[:1000] or "(no output)",
            }
        elif auth_issue:
            return {
                "source": source, "success": False,
                "message": auth_issue,
                "details": (stderr or stdout or "")[:2000],
            }
        else:
            # Show last portion of stderr (gallery-dl puts the actual error at the end)
            detail = (stderr or stdout or "").strip()
            if len(detail) > 2000:
                detail = "...(truncated)\n" + detail[-2000:]
            return {
                "source": source, "success": False,
                "message": f"gallery-dl exited with code {result.returncode}",
                "details": detail or "(no output — check gallery-dl version and network connectivity)",
            }

    except subprocess.TimeoutExpired:
        return {
            "source": source, "success": False,
            "message": "Connection test timed out after 120s",
            "details": "The request to the remote server took too long.",
        }
    except Exception:
        logger.exception("Connection test failed for %s", source)
        return {
            "source": source, "success": False,
            "message": "Connection test failed unexpectedly.",
            "details": "Check the backend logs for the request details.",
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
