# Showcase Homepage + Universal Slideshow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `/` redirect-to-dashboard with an immersive, work-first showcase homepage (makemepulse-style mouse-trail imagery, WebGL-enhanced with a first-class DOM fallback), add a universal fullscreen slideshow launchable from three pages, and put both under user-level settings.

**Architecture:** A new `library`-gated backend endpoint samples random works via a random-offset window (index scan, not `ORDER BY random()`) and returns pre-signed preview URLs plus asset dimensions. The frontend ships the DOM trail first and layers WebGL (lazy-loaded `ogl`) on top, switching by an explicit degradation contract. All config lives in the existing user-`preferences` sync under a new `showcase` key.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async; Next.js 14 App Router + TypeScript + Tailwind; TanStack Query; `ogl` (new, lazy); existing `src/lib/motion` primitives.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-20-showcase-homepage-design.md` — normative for behavior, config shape, and degradation.
- Showcase endpoint carries `RequirePermission("library")`; NSFW is double-gated: `force_sfw = not user.nsfw_visible or not include_nsfw`. The account setting always wins.
- Sampling must NOT use `ORDER BY random()` (full scan on ~36k works). Use a random-offset window over `WorkRepository.list_all`.
- Motion rules (from `docs/frontend-motion-audit.md`, non-negotiable): animate only `transform`/`opacity`; all durations/easings from `src/lib/motion/tokens.ts`; `shouldAnimate()` gates at the source; `prefers-reduced-motion` is highest priority.
- Degradation contract (all four required): reduced-motion → static grid, no `ogl` fetch; `hardwareConcurrency <= 4` → DOM trail; WebGL create-fail or `webglcontextlost` → silent DOM fallback; `document.hidden` → pause rAF.
- `ogl` MUST be dynamically imported so no other route's bundle grows (mirrors the `animejs` lazy chunk in `src/lib/motion/anime.ts`).
- `PUT /me/preferences` **whole-replaces** the preferences object — any new key must also be added to `readFullPreferencesFromLocalStorage()` in `admin-web/src/lib/preferencesSync.ts`, or it will be silently wiped by the next theme/lang change.
- Every user-visible string goes through `t()` with keys added to BOTH `zh` and `en` dicts in `admin-web/src/lib/i18n.tsx`.
- Backend tests: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest <path> -q`. Known unrelated flake: `tests/test_disk_import.py::test_reconcile_downloads_to_db_registers_and_enqueues_idempotently`.
- Frontend verify: `cd admin-web && npx tsc --noEmit && npm run build`.
- Deploy: `docker compose build --build-arg CACHEBUST="$(date +%s)" <services> && docker compose up -d --force-recreate <services>`.
- **Environment note:** host port 13000 is occupied by a non-Docker Windows process (returns 502). admin-web is currently published on **18790** — use `http://127.0.0.1:18790` for all manual/perf verification until 13000 is freed.
- Commit messages end with:
  ```
  Co-Authored-By: Claude <noreply@anthropic.com>
  ```

---

## File Structure

**Backend**
| File | Responsibility |
|---|---|
| `backend/app/schemas/showcase.py` (new) | `ShowcaseItem`, `ShowcaseSampleResponse` Pydantic models |
| `backend/app/api/showcase.py` (new) | `GET /sample` route: filters → random window → signed URLs |
| `backend/app/api/__init__.py` (modify) | register showcase router |
| `backend/app/api/auth_api.py` (modify, 1 line) | add `"showcase"` to `_ALLOWED_PREFERENCE_KEYS` |
| `backend/tests/test_showcase_api.py` (new) | sampling, NSFW gate, filters, signed-URL validity, empty library |

**Frontend**
| File | Responsibility |
|---|---|
| `admin-web/src/lib/showcase/config.tsx` (new) | `ShowcaseConfig` type, defaults, provider/hook, localStorage + `pushPreferences` |
| `admin-web/src/lib/preferencesSync.ts` (modify) | add `SHOWCASE_KEY` to the full-payload reader |
| `admin-web/src/lib/showcase/trail.ts` (new) | pure trail state machine (spawn/lifetime/cap), shared by DOM + WebGL |
| `admin-web/src/lib/showcase/webgl.ts` (new) | sole `ogl` entry: renderer, texture pool, rAF loop |
| `admin-web/src/components/showcase/ShowcaseHero.tsx` (new) | headline, stats, entry links |
| `admin-web/src/components/showcase/ShowcaseTrailDOM.tsx` (new) | DOM/transform trail (first-class fallback) |
| `admin-web/src/components/showcase/ShowcaseCanvas.tsx` (new) | WebGL layer + degradation switch |
| `admin-web/src/components/showcase/ShowcaseEmpty.tsx` (new) | empty-library guidance |
| `admin-web/src/components/SlideshowPlayer.tsx` (new) | fullscreen player (Ken Burns / crossfade, keyboard, autoplay) |
| `admin-web/src/lib/useSlideshow.ts` (new) | launcher hook shared by the three entry points |
| `admin-web/src/app/page.tsx` (rewrite) | showcase assembly (thin) |
| `admin-web/src/app/admin/settings/showcase/page.tsx` (new) | four config groups |
| `admin-web/src/lib/api/index.ts` + `types.ts` (modify) | `api.showcaseSample()`, `ShowcaseItem` type, `queryKeys.showcase` |

---

### Task 1: Backend showcase sampling endpoint

**Files:**
- Create: `backend/app/schemas/showcase.py`
- Create: `backend/app/api/showcase.py`
- Modify: `backend/app/api/__init__.py`
- Test: `backend/tests/test_showcase_api.py`

**Interfaces:**
- Consumes: `WorkRepository.list_all(offset, limit, source, tag, is_favorite, curation_visibility, precomputed_total, force_sfw) -> tuple[list[Work], int]`; `signed_media_url(asset_id: str, size: str, ttl_seconds: int = 600) -> str`; `cache_get/cache_key/cache_set` from `app.services.cache`; `RequirePermission("library")` from `app.auth`.
- Produces: `GET /api/v1/showcase/sample?count=&scope=&source=&tag=&include_nsfw=` returning
  `{"items": [{"work_id": str, "title": str|None, "creator_name": str|None, "source": str|None, "thumb_url": str, "preview_url": str, "width": int|None, "height": int|None}]}`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_showcase_api.py`:

```python
"""Showcase sampling endpoint: auth gate, NSFW double-gate, filters, signed URLs."""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

PREFIX = "showcase_api_"


async def _clear(db):
    from app.services.cache import cache_delete_pattern

    await db.execute(text(f"DELETE FROM users WHERE username LIKE '{PREFIX}%'"))
    await db.execute(text(
        "TRUNCATE work_sources, works, assets, source_creators, creators RESTART IDENTITY CASCADE"))
    await db.commit()
    cache_delete_pattern("works:*")
    cache_delete_pattern("showcase:*")


async def _seed_user(db, username, *, nsfw_visible=True, permissions=None):
    from app.auth import hash_password
    from app.models.user import User

    user = User(
        username=username,
        password_hash=hash_password("hunter22"),
        is_admin=False,
        is_active=True,
        permissions=permissions if permissions is not None else ["library"],
        nsfw_visible=nsfw_visible,
        must_change_password=False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _seed_works(db):
    """One SFW work with a sized thumbnail asset, one NSFW work, one favorite."""
    from app.models import Work
    from app.models.asset import Asset

    sfw = Work(title="sfw showcase", is_nsfw=False, is_favorite=False)
    nsfw = Work(title="nsfw showcase", is_nsfw=True, is_favorite=False)
    fav = Work(title="fav showcase", is_nsfw=False, is_favorite=True)
    db.add_all([sfw, nsfw, fav])
    await db.commit()
    for w in (sfw, nsfw, fav):
        await db.refresh(w)

    asset = Asset(work_id=sfw.id, file_path=f"/x/{sfw.id}.jpg", file_name="a.jpg",
                  width=1200, height=1600, mime_type="image/jpeg")
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    sfw.thumbnail_asset_id = asset.id
    await db.commit()
    return sfw, nsfw, fav, asset


def _headers(username):
    from app.auth import create_access_token
    return {"Authorization": f"Bearer {create_access_token(username, must_change_password=False)}"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sample_hides_nsfw_from_restricted_user_and_signs_preview_urls():
    from urllib.parse import parse_qs, urlparse

    from app.database import async_session, engine
    from app.main import app
    from app.services.media_signing import verify_media_token

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}restricted", nsfw_visible=False)
            sfw, nsfw, fav, asset = await _seed_works(db)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/v1/showcase/sample?count=10",
                                 headers=_headers(f"{PREFIX}restricted"))
            assert r.status_code == 200, r.text
            items = r.json()["items"]
            ids = {i["work_id"] for i in items}
            assert str(sfw.id) in ids
            assert str(nsfw.id) not in ids

            item = next(i for i in items if i["work_id"] == str(sfw.id))
            assert item["width"] == 1200 and item["height"] == 1600
            assert item["thumb_url"] == f"/media/thumb/{asset.id}"

            parsed = urlparse(item["preview_url"])
            q = parse_qs(parsed.query)
            assert parsed.path == f"/media/preview/{asset.id}"
            assert verify_media_token(str(asset.id), "preview",
                                      q["expires"][0], q["token"][0]) is True
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sample_respects_favorites_scope_and_count_cap():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}user2", nsfw_visible=True)
            sfw, nsfw, fav, _asset = await _seed_works(db)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/v1/showcase/sample?count=10&scope=favorites",
                                 headers=_headers(f"{PREFIX}user2"))
            assert r.status_code == 200, r.text
            ids = {i["work_id"] for i in r.json()["items"]}
            assert ids == {str(fav.id)}

            r2 = await client.get("/api/v1/showcase/sample?count=2",
                                  headers=_headers(f"{PREFIX}user2"))
            assert r2.status_code == 200, r2.text
            assert len(r2.json()["items"]) <= 2
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sample_empty_library_returns_empty_list_and_requires_permission():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}empty", nsfw_visible=True)
            await _seed_user(db, f"{PREFIX}nolib", permissions=["tasks"])

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/v1/showcase/sample?count=5",
                                 headers=_headers(f"{PREFIX}empty"))
            assert r.status_code == 200, r.text
            assert r.json()["items"] == []

            r2 = await client.get("/api/v1/showcase/sample?count=5",
                                  headers=_headers(f"{PREFIX}nolib"))
            assert r2.status_code == 403

            r3 = await client.get("/api/v1/showcase/sample?count=5")
            assert r3.status_code == 401
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
```

Note: `_seed_works` writes only the columns this test needs. If `Work` or `Asset` has additional NOT NULL columns without defaults, read `backend/app/models/work.py` and `backend/app/models/asset.py` and add them — do not weaken the assertions.

- [ ] **Step 2: Run the test to verify it fails**

Run: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest tests/test_showcase_api.py -q`
Expected: FAIL — all three tests get 404 (route not registered).

- [ ] **Step 3: Write the response schemas**

Create `backend/app/schemas/showcase.py`:

```python
from pydantic import BaseModel


class ShowcaseItem(BaseModel):
    work_id: str
    title: str | None = None
    creator_name: str | None = None
    source: str | None = None
    thumb_url: str
    preview_url: str
    width: int | None = None
    height: int | None = None


class ShowcaseSampleResponse(BaseModel):
    items: list[ShowcaseItem]
```

- [ ] **Step 4: Write the endpoint**

Create `backend/app/api/showcase.py`:

```python
"""Showcase sampling: a random window of visible works with signed preview URLs.

Deliberately avoids `ORDER BY random()` — that is a full scan of a ~36k-row
table. Instead: read the (cached) filtered total, pick a random offset, pull
one indexed window via the existing repository, then shuffle within the
window. Randomness comes from a fresh window per request plus the in-window
shuffle; cost stays an index scan.
"""
import random
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequirePermission
from app.database import get_db
from app.models.asset import Asset
from app.models.user import User
from app.repositories.work import WorkRepository
from app.schemas.showcase import ShowcaseItem, ShowcaseSampleResponse
from app.services.cache import cache_get, cache_key, cache_set
from app.services.media_signing import signed_media_url

_require_library = RequirePermission("library")

router = APIRouter()


@router.get("/sample", response_model=ShowcaseSampleResponse)
async def sample(
    count: int = Query(24, ge=1, le=60),
    scope: str = Query("all", pattern="^(all|favorites)$"),
    source: str | None = None,
    tag: str | None = None,
    include_nsfw: bool = False,
    user: User = _require_library,
    db: AsyncSession = Depends(get_db),
):
    # NSFW is double-gated: the account setting always wins over the request.
    force_sfw = (not user.nsfw_visible) or (not include_nsfw)
    is_favorite = True if scope == "favorites" else None

    count_ck = cache_key("showcase:count", source=source, tag=tag,
                         is_favorite=is_favorite, force_sfw=force_sfw)
    cached_total = cache_get(count_ck)

    repo = WorkRepository(db)
    # The first call establishes the total and doubles as the window when the
    # filtered library is smaller than `count`.
    works, total = await repo.list_all(
        offset=0, limit=count,
        source=source, tag=tag, is_favorite=is_favorite,
        curation_visibility="visible",
        precomputed_total=cached_total,
        force_sfw=force_sfw,
    )
    if cached_total is None:
        cache_set(count_ck, total, 300)

    if total > count:
        offset = random.randint(0, total - count)
        works, _ = await repo.list_all(
            offset=offset, limit=count,
            source=source, tag=tag, is_favorite=is_favorite,
            curation_visibility="visible",
            precomputed_total=total,
            force_sfw=force_sfw,
        )

    works = list(works)
    random.shuffle(works)

    asset_ids = [w.thumbnail_asset_id for w in works if w.thumbnail_asset_id]
    dims: dict[UUID, tuple[int | None, int | None]] = {}
    if asset_ids:
        rows = (await db.execute(
            select(Asset.id, Asset.width, Asset.height).where(Asset.id.in_(asset_ids))
        )).all()
        dims = {r.id: (r.width, r.height) for r in rows}

    items: list[ShowcaseItem] = []
    for w in works:
        if not w.thumbnail_asset_id:
            continue  # nothing to render — skip rather than emit a blank plane
        aid = str(w.thumbnail_asset_id)
        width, height = dims.get(w.thumbnail_asset_id, (None, None))
        items.append(ShowcaseItem(
            work_id=str(w.id),
            title=w.title,
            creator_name=getattr(w, "creator_name", None),
            source=getattr(w, "source", None),
            thumb_url=f"/media/thumb/{aid}",
            preview_url=signed_media_url(aid, "preview"),
            width=width,
            height=height,
        ))

    return ShowcaseSampleResponse(items=items)
```

- [ ] **Step 5: Register the router**

In `backend/app/api/__init__.py`, add the import beside the other route imports and the include beside the other `include_router` calls:

```python
from app.api.showcase import router as showcase_router
```
```python
api_router.include_router(showcase_router, prefix="/showcase", tags=["showcase"])
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest tests/test_showcase_api.py -q`
Expected: PASS — `3 passed`.

- [ ] **Step 7: Run the full suite, then deploy**

Run: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest -q`
Expected: all pass except the known `test_disk_import.py` flake.

```bash
docker compose build --build-arg CACHEBUST="$(date +%s)" backend
docker compose up -d --force-recreate backend worker-download worker-import worker-operations scheduler
```

- [ ] **Step 8: Commit**

```bash
git add backend/app/schemas/showcase.py backend/app/api/showcase.py backend/app/api/__init__.py backend/tests/test_showcase_api.py
git commit -m "feat(showcase): random-window sampling endpoint with signed preview URLs

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Showcase preference plumbing

**Files:**
- Modify: `backend/app/api/auth_api.py` (`_ALLOWED_PREFERENCE_KEYS`, line 27)
- Create: `admin-web/src/lib/showcase/config.tsx`
- Modify: `admin-web/src/lib/preferencesSync.ts`
- Modify: `admin-web/src/app/providers.tsx`
- Test: `backend/tests/test_users_api.py` (add one test)

**Interfaces:**
- Consumes: `pushPreferences(partial)` from `@/lib/preferencesSync`; `PUT /me/preferences` whole-replace semantics.
- Produces: `useShowcaseConfig(): { config: ShowcaseConfig; update: (patch: Partial<ShowcaseConfig>) => void }`; exported `ShowcaseConfig`, `DEFAULT_SHOWCASE_CONFIG`, `SHOWCASE_STORAGE_KEY = "auto-gallery-showcase-v1"`, `applyShowcasePreferences(value: unknown): void`, `ShowcaseConfigProvider`.

- [ ] **Step 1: Write the failing backend test**

Append to `backend/tests/test_users_api.py`, reusing that file's existing `PREFIX`, `_clear`, `_seed_user`, `_headers` helpers:

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_preferences_accepts_showcase_key():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}prefs_showcase", is_admin=True)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            h = _headers(f"{PREFIX}prefs_showcase")
            body = {"preferences": {"theme": "dark",
                                    "showcase": {"scope": "favorites", "trailMax": 12}}}
            r = await client.put("/api/v1/auth/me/preferences", json=body, headers=h)
            assert r.status_code == 200, r.text
            assert r.json()["preferences"]["showcase"]["scope"] == "favorites"

            me = await client.get("/api/v1/auth/me", headers=h)
            assert me.json()["preferences"]["showcase"]["trailMax"] == 12
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest tests/test_users_api.py::test_put_preferences_accepts_showcase_key -q`
Expected: FAIL — 400 with `invalid preference key(s): showcase`.

- [ ] **Step 3: Widen the backend whitelist**

`backend/app/api/auth_api.py` line 27:

```python
_ALLOWED_PREFERENCE_KEYS = {"theme", "palette", "lang", "appearance", "showcase"}
```

- [ ] **Step 4: Run it to verify it passes**

Run: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest tests/test_users_api.py -q`
Expected: PASS (whole file green).

- [ ] **Step 5: Create the frontend config module**

Create `admin-web/src/lib/showcase/config.tsx`:

```tsx
"use client";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { pushPreferences } from "@/lib/preferencesSync";

export const SHOWCASE_STORAGE_KEY = "auto-gallery-showcase-v1";

export interface ShowcaseConfig {
  // content source
  scope: "all" | "favorites";
  source: string | null;
  tag: string | null;
  includeNsfw: boolean;
  // motion
  trailMax: number;
  spawnIntervalMs: number;
  followDamping: number;
  parallaxStrength: number;
  minimal: boolean;
  // slideshow
  slideDwellMs: number;
  slideTransition: "crossfade" | "kenburns";
  slideLoop: boolean;
  slideShowMeta: boolean;
  // homepage behavior
  landing: "showcase" | "dashboard";
  headline: string;
  showStats: boolean;
}

export const DEFAULT_SHOWCASE_CONFIG: ShowcaseConfig = {
  scope: "all",
  source: null,
  tag: null,
  includeNsfw: false,
  trailMax: 18,
  spawnIntervalMs: 90,
  followDamping: 0.12,
  parallaxStrength: 0.4,
  minimal: false,
  slideDwellMs: 5000,
  slideTransition: "kenburns",
  slideLoop: true,
  slideShowMeta: true,
  landing: "showcase",
  headline: "",
  showStats: true,
};

function readStored(): ShowcaseConfig {
  if (typeof window === "undefined") return DEFAULT_SHOWCASE_CONFIG;
  try {
    const raw = window.localStorage.getItem(SHOWCASE_STORAGE_KEY);
    return raw ? { ...DEFAULT_SHOWCASE_CONFIG, ...JSON.parse(raw) } : DEFAULT_SHOWCASE_CONFIG;
  } catch {
    return DEFAULT_SHOWCASE_CONFIG;
  }
}

/** Apply server-side preferences over localStorage (called by the hydrator). */
export function applyShowcasePreferences(value: unknown): void {
  if (typeof window === "undefined" || !value || typeof value !== "object") return;
  const merged = { ...readStored(), ...(value as Partial<ShowcaseConfig>) };
  try {
    window.localStorage.setItem(SHOWCASE_STORAGE_KEY, JSON.stringify(merged));
  } catch {}
  window.dispatchEvent(new CustomEvent("ag:showcase-config"));
}

interface Ctx {
  config: ShowcaseConfig;
  update: (patch: Partial<ShowcaseConfig>) => void;
}

const ShowcaseConfigContext = createContext<Ctx>({
  config: DEFAULT_SHOWCASE_CONFIG,
  update: () => {},
});

export function useShowcaseConfig(): Ctx {
  return useContext(ShowcaseConfigContext);
}

export function ShowcaseConfigProvider({ children }: { children: ReactNode }) {
  // Start from defaults so server and first client render match, then adopt
  // localStorage in an effect (same pattern as the appearance provider).
  const [config, setConfig] = useState<ShowcaseConfig>(DEFAULT_SHOWCASE_CONFIG);

  useEffect(() => {
    setConfig(readStored());
    const onExternal = () => setConfig(readStored());
    window.addEventListener("ag:showcase-config", onExternal);
    return () => window.removeEventListener("ag:showcase-config", onExternal);
  }, []);

  const update = useCallback((patch: Partial<ShowcaseConfig>) => {
    setConfig((prev) => {
      const next = { ...prev, ...patch };
      try {
        localStorage.setItem(SHOWCASE_STORAGE_KEY, JSON.stringify(next));
      } catch {}
      pushPreferences({ showcase: next });
      return next;
    });
  }, []);

  return (
    <ShowcaseConfigContext.Provider value={{ config, update }}>
      {children}
    </ShowcaseConfigContext.Provider>
  );
}
```

- [ ] **Step 6: Add the key to the whole-replace payload reader**

`PUT /me/preferences` replaces the entire object, so a showcase write would be wiped by the next theme change unless the reader includes it. In `admin-web/src/lib/preferencesSync.ts`, add the constant beside the other storage keys:

```ts
const SHOWCASE_KEY = "auto-gallery-showcase-v1";
```

and extend `readFullPreferencesFromLocalStorage()`:

```ts
function readFullPreferencesFromLocalStorage(): Record<string, unknown> {
  try {
    const appearanceRaw = localStorage.getItem(APPEARANCE_KEY);
    const showcaseRaw = localStorage.getItem(SHOWCASE_KEY);
    return {
      theme: localStorage.getItem(THEME_KEY) || "system",
      palette: localStorage.getItem(PALETTE_KEY) || "github",
      lang: localStorage.getItem(LANG_KEY) || "zh",
      appearance: appearanceRaw ? JSON.parse(appearanceRaw) : {},
      showcase: showcaseRaw ? JSON.parse(showcaseRaw) : {},
    };
  } catch {
    return {};
  }
}
```

Keep the existing defaults for `theme`/`palette`/`lang` exactly as the file already has them — only the two new lines are additions.

- [ ] **Step 7: Mount the provider and hydrate from the server**

In `admin-web/src/app/providers.tsx`:

```tsx
import { ShowcaseConfigProvider, applyShowcasePreferences } from "@/lib/showcase/config";
```

Wrap the tree with `<ShowcaseConfigProvider>` alongside the existing theme/appearance providers, and inside the existing effect that applies `me.preferences`, add:

```tsx
if (prefs.showcase) applyShowcasePreferences(prefs.showcase);
```

- [ ] **Step 8: Verify, deploy, commit**

Run: `cd admin-web && npx tsc --noEmit && npm run build`
Expected: `✓ Compiled successfully`.

```bash
docker compose build --build-arg CACHEBUST="$(date +%s)" backend admin-web
docker compose up -d --force-recreate backend worker-download worker-import worker-operations scheduler admin-web
git add backend/app/api/auth_api.py backend/tests/test_users_api.py admin-web/src/lib/showcase/config.tsx admin-web/src/lib/preferencesSync.ts admin-web/src/app/providers.tsx
git commit -m "feat(showcase): showcase preference key, config provider, server sync

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Showcase settings page

**Files:**
- Create: `admin-web/src/app/admin/settings/showcase/page.tsx`
- Modify: `admin-web/src/app/admin/settings/page.tsx`
- Modify: `admin-web/src/lib/i18n.tsx`

**Interfaces:**
- Consumes: `useShowcaseConfig()` (Task 2); `api.sources()` for the source list; the current user's `nsfw_visible`; existing shell components (`PageHeader`, `SectionPanel`/`Card`, `PermissionGuard`) — match whatever `admin-web/src/app/admin/settings/appearance/page.tsx` already uses.
- Produces: route `/admin/settings/showcase`.

- [ ] **Step 1: Read the appearance settings page as the template**

Read `admin-web/src/app/admin/settings/appearance/page.tsx` in full before writing. The showcase page must reuse its exact page shell, section/panel components, toggle and segmented-control markup, and its write-through (no save button) interaction model.

- [ ] **Step 2: Build the page**

Create `admin-web/src/app/admin/settings/showcase/page.tsx`, wrapped in `<PermissionGuard module="system">`, with four sections. Every control calls `update({ ... })` immediately — no local draft state, no save button.

**① 内容源 (`showcase_settings.group_source`)**
- `scope`: segmented — `all` / `favorites`
- `source`: `<select>` with an "任意" option (`null`) plus one option per `api.sources()` entry
- `tag`: text input; on change, empty string → `null`
- `includeNsfw`: toggle. **Disabled with the hint `showcase_settings.include_nsfw_locked` when the current user's `nsfw_visible === false`** — the account setting hard-limits it, so a live-looking toggle would lie.

**② 动效 (`showcase_settings.group_motion`)**
- `trailMax`: range 4–40, step 1
- `spawnIntervalMs`: range 40–400, step 10
- `followDamping`: range 0.02–0.5, step 0.01
- `parallaxStrength`: range 0–1, step 0.05
- `minimal`: toggle; when on, the four controls above render `disabled` with the hint `showcase_settings.minimal_hint` (they have no effect in minimal mode)

**③ 幻灯片 (`showcase_settings.group_slideshow`)**
- `slideDwellMs`: range 2000–15000, step 500
- `slideTransition`: segmented — `kenburns` / `crossfade`
- `slideLoop`, `slideShowMeta`: toggles

**④ 首页行为 (`showcase_settings.group_home`)**
- `landing`: segmented — `showcase` / `dashboard`
- `headline`: text input, placeholder = `t("showcase.headline_default")`
- `showStats`: toggle

Each range control shows its current value next to its label using the existing tabular-numeral class so the digits don't jitter while dragging.

- [ ] **Step 3: Add the settings-index entry**

In `admin-web/src/app/admin/settings/page.tsx`, add a card linking to `/admin/settings/showcase` in the same shape as the existing appearance card: title `t("showcase_settings.title")`, description `t("showcase_settings.desc")`.

- [ ] **Step 4: Add i18n keys (zh AND en)**

Add to both dictionaries in `admin-web/src/lib/i18n.tsx`:

`showcase_settings.title`, `.desc`, `.group_source`, `.group_motion`, `.group_slideshow`, `.group_home`, `.scope`, `.scope_all`, `.scope_favorites`, `.source`, `.source_any`, `.tag`, `.tag_placeholder`, `.include_nsfw`, `.include_nsfw_locked`, `.trail_max`, `.spawn_interval`, `.follow_damping`, `.parallax`, `.minimal`, `.minimal_hint`, `.slide_dwell`, `.slide_transition`, `.transition_kenburns`, `.transition_crossfade`, `.slide_loop`, `.slide_meta`, `.landing`, `.landing_showcase`, `.landing_dashboard`, `.headline`, `.show_stats`

- [ ] **Step 5: Verify, deploy, commit**

Run: `cd admin-web && npx tsc --noEmit && npm run build`
Expected: green; `/admin/settings/showcase` appears in the printed route table.

```bash
docker compose build --build-arg CACHEBUST="$(date +%s)" admin-web && docker compose up -d --force-recreate admin-web
git add admin-web/src/app/admin/settings/showcase/page.tsx admin-web/src/app/admin/settings/page.tsx admin-web/src/lib/i18n.tsx
git commit -m "feat(admin-web): showcase settings page

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Showcase page skeleton — DOM trail, hero, states

Ships a **fully usable** showcase with zero WebGL. Task 5 layers WebGL on top of this, so the fallback path is exercised from day one rather than bolted on.

**Files:**
- Rewrite: `admin-web/src/app/page.tsx`
- Create: `admin-web/src/lib/showcase/trail.ts`
- Create: `admin-web/src/components/showcase/ShowcaseHero.tsx`
- Create: `admin-web/src/components/showcase/ShowcaseTrailDOM.tsx`
- Create: `admin-web/src/components/showcase/ShowcaseEmpty.tsx`
- Modify: `admin-web/src/lib/api/types.ts`, `admin-web/src/lib/api/index.ts`
- Modify: `admin-web/src/middleware.ts`
- Modify: `admin-web/src/app/admin/login/page.tsx`
- Modify: `admin-web/src/lib/i18n.tsx`

**Interfaces:**
- Consumes: `api.showcaseSample(params)`; `useShowcaseConfig()`; `motionConfig.shouldAnimate()` from `@/lib/motion`.
- Produces:
  ```ts
  export interface TrailItem { id: number; x: number; y: number; bornAt: number; imageIndex: number }
  export interface TrailController {
    pointerMove(x: number, y: number): void;  // throttled internally by spawnIntervalMs
    tick(now: number): TrailItem[];           // returns live items, culls expired
    reset(): void;
  }
  export function createTrail(opts: {
    max: number; spawnIntervalMs: number; lifetimeMs: number; imageCount: number;
  }): TrailController
  ```
  plus `<ShowcaseTrailDOM items={ShowcaseItem[]} config={...} />` consumed by Task 5.

- [ ] **Step 1: Add the typed API client method**

In `admin-web/src/lib/api/types.ts`:

```ts
export interface ShowcaseItem {
  work_id: string;
  title: string | null;
  creator_name: string | null;
  source: string | null;
  thumb_url: string;
  preview_url: string;
  width: number | null;
  height: number | null;
}

export interface ShowcaseSampleResponse {
  items: ShowcaseItem[];
}
```

In `admin-web/src/lib/api/index.ts`, add the method beside the other `request<T>` calls (match the file's existing import alias for the types module):

```ts
showcaseSample: (params: {
  count?: number; scope?: string; source?: string | null; tag?: string | null; include_nsfw?: boolean;
} = {}) => {
  const q = new URLSearchParams();
  if (params.count) q.set("count", String(params.count));
  if (params.scope) q.set("scope", params.scope);
  if (params.source) q.set("source", params.source);
  if (params.tag) q.set("tag", params.tag);
  if (params.include_nsfw) q.set("include_nsfw", "true");
  return request<ShowcaseSampleResponse>(`/api/v1/showcase/sample?${q.toString()}`);
},
```

and register the query key beside the others:

```ts
showcase: {
  sample: (params: Record<string, unknown>) => ["showcase", "sample", params] as const,
},
```

- [ ] **Step 2: Write the trail state machine**

Create `admin-web/src/lib/showcase/trail.ts` — pure, no DOM and no React, so the DOM and WebGL renderers drive identical logic:

```ts
export interface TrailItem {
  id: number;
  x: number;
  y: number;
  bornAt: number;
  imageIndex: number;
}

export interface TrailController {
  pointerMove(x: number, y: number): void;
  tick(now: number): TrailItem[];
  reset(): void;
}

export function createTrail(opts: {
  max: number;
  spawnIntervalMs: number;
  lifetimeMs: number;
  imageCount: number;
}): TrailController {
  let items: TrailItem[] = [];
  let lastSpawn = 0;
  let nextId = 1;
  let cursor = 0;

  return {
    pointerMove(x, y) {
      if (opts.imageCount <= 0) return;
      const now = performance.now();
      if (now - lastSpawn < opts.spawnIntervalMs) return;
      lastSpawn = now;
      items.push({ id: nextId++, x, y, bornAt: now, imageIndex: cursor % opts.imageCount });
      cursor++;
      if (items.length > opts.max) items = items.slice(items.length - opts.max);
    },
    tick(now) {
      items = items.filter((it) => now - it.bornAt < opts.lifetimeMs);
      return items;
    },
    reset() {
      items = [];
      lastSpawn = 0;
    },
  };
}
```

- [ ] **Step 3: Build the DOM trail renderer**

Create `admin-web/src/components/showcase/ShowcaseTrailDOM.tsx` (`"use client"`). Requirements:

- Props: `{ items: ShowcaseItem[]; config: Pick<ShowcaseConfig, "trailMax" | "spawnIntervalMs" | "followDamping" | "parallaxStrength"> }`
- **Reduced-motion / low-end path:** if `!motionConfig.shouldAnimate()`, render a static centered grid of the first 8 items and return early — no rAF, no pointer listener, no trail controller.
- Otherwise: an absolutely-positioned `<img>` pool of exactly `config.trailMax` elements created once via refs. A single rAF loop calls `controller.tick(performance.now())` and writes, per live item, **only** `transform: translate3d(Xpx, Ypx, 0) scale(S)` and `opacity` — never `left`/`top`/`width`/`height`. Unused pool slots get `opacity: 0`.
- Scale and opacity derive from item age: fade in over the first ~120 ms, hold, then fade out across the remaining lifetime (`lifetimeMs = config.trailMax * config.spawnIntervalMs`, clamped to at least 900 ms).
- `followDamping` lerps the rendered pointer position toward the raw pointer each frame; `parallaxStrength` offsets items by a fraction of the pointer delta so the layer feels deeper.
- `pointermove` on the container feeds `controller.pointerMove(e.clientX - rect.left, e.clientY - rect.top)`.
- rAF pauses while `document.hidden` (`visibilitychange` listener) and is cancelled on unmount along with all listeners.
- Each `<img>` uses `src={item.thumb_url}`, `decoding="async"`, `alt=""`, `aria-hidden="true"`, plus `width`/`height` attributes from the item when present so nothing shifts.

- [ ] **Step 4: Build hero and empty states**

Create `admin-web/src/components/showcase/ShowcaseHero.tsx`: the headline (`config.headline` when non-empty, otherwise `t("showcase.headline_default")`), an optional stat line when `config.showStats`, and entry links — 仪表盘 `/admin`, 图库 `/admin/works`, and 上传 `/admin/upload` rendered only when `has("upload")`. Use the existing fluid type-scale classes; introduce no new font.

Create `admin-web/src/components/showcase/ShowcaseEmpty.tsx`: the existing `EmptyState` component with `t("showcase.empty_title")` / `t("showcase.empty_desc")` and links to `/admin/subscriptions` and `/admin/upload`.

- [ ] **Step 5: Assemble the page**

Rewrite `admin-web/src/app/page.tsx` (`"use client"`, replacing the current `redirect("/admin")`):

- Read `const { config } = useShowcaseConfig()`.
- `useQuery({ queryKey: queryKeys.showcase.sample(params), queryFn: () => api.showcaseSample(params), staleTime: 5 * 60_000 })` where `params = { count: 24, scope: config.scope, source: config.source, tag: config.tag, include_nsfw: config.includeNsfw }`.
- Render order: loading → the page's existing skeleton idiom; error → `ErrorState` with a retry that calls `refetch()`; `items.length === 0` → `<ShowcaseEmpty />`; otherwise a `relative min-h-screen overflow-hidden` shell containing `<ShowcaseTrailDOM items={items} config={config} />` behind `<ShowcaseHero />`.

- [ ] **Step 6: Wire landing behavior**

`admin-web/src/middleware.ts` — the already-authenticated visitor to the login page should land on the new home:

```ts
if (pathname === LOGIN && token) {
  return NextResponse.redirect(new URL("/", request.url));
}
```

`admin-web/src/app/admin/login/page.tsx` — after a successful login, honor the `landing` preference. The login page runs before preferences are hydrated, so read the same localStorage key directly:

```ts
const landing = (() => {
  try {
    const raw = localStorage.getItem("auto-gallery-showcase-v1");
    return raw && JSON.parse(raw).landing === "dashboard" ? "/admin" : "/";
  } catch {
    return "/";
  }
})();
router.replace(landing);
```

Replace the existing post-login navigation call with this; leave the must-change-password branch exactly as it is.

- [ ] **Step 7: Add i18n keys (zh AND en)**

`showcase.headline_default`, `showcase.stats`, `showcase.enter_gallery`, `showcase.enter_dashboard`, `showcase.enter_upload`, `showcase.empty_title`, `showcase.empty_desc`

- [ ] **Step 8: Verify, deploy, commit**

Run: `cd admin-web && npx tsc --noEmit && npm run build`
Expected: green, with `/` listed in the route table as a client route (no longer a redirect).

Manual on `http://127.0.0.1:18790/`: the trail follows the pointer; `/admin` still renders the dashboard unchanged; with OS reduced-motion enabled, `/` shows the static grid and the pointer produces no new elements.

```bash
docker compose build --build-arg CACHEBUST="$(date +%s)" admin-web && docker compose up -d --force-recreate admin-web
git add admin-web/src/app/page.tsx admin-web/src/lib/showcase/trail.ts admin-web/src/components/showcase admin-web/src/lib/api admin-web/src/middleware.ts admin-web/src/app/admin/login/page.tsx admin-web/src/lib/i18n.tsx
git commit -m "feat(showcase): homepage with DOM pointer trail, hero, and landing routing

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: WebGL layer + degradation switch

**Files:**
- Create: `admin-web/src/lib/showcase/webgl.ts`
- Create: `admin-web/src/components/showcase/ShowcaseCanvas.tsx`
- Modify: `admin-web/src/app/page.tsx`
- Modify: `admin-web/package.json`, `admin-web/package-lock.json`

**Interfaces:**
- Consumes: `createTrail()` / `TrailItem` from Task 4; `<ShowcaseTrailDOM>` from Task 4; `motionConfig.shouldAnimate()`.
- Produces:
  ```ts
  export interface ShowcaseRenderer {
    setImages(urls: string[]): Promise<void>;
    render(items: TrailItem[], pointer: { x: number; y: number }): void;
    resize(): void;
    destroy(): void;
  }
  export function createShowcaseRenderer(
    canvas: HTMLCanvasElement,
    opts: { maxTextures: number; parallaxStrength: number },
  ): Promise<ShowcaseRenderer>
  ```
  Rejects when WebGL is unavailable — the caller falls back to DOM.

- [ ] **Step 1: Install ogl**

Run: `cd admin-web && npm install ogl`
Expected: `added 1 package`.

- [ ] **Step 2: Write the renderer**

Create `admin-web/src/lib/showcase/webgl.ts` — the **only** module permitted to reference `ogl`, and it must use a dynamic `import("ogl")` so the dependency lands in its own lazy chunk (same technique as `src/lib/motion/anime.ts`):

- `createShowcaseRenderer(canvas, opts)`:
  - `const { Renderer, Camera, Transform, Plane, Program, Mesh, Texture } = await import("ogl");`
  - Construct `new Renderer({ canvas, alpha: true, dpr: Math.min(window.devicePixelRatio || 1, 2) })` inside `try/catch`; if it throws or `renderer.gl` is falsy, `throw new Error("webgl unavailable")` so the caller degrades.
  - Build one shared `Plane` geometry and one `Program`; each pooled mesh gets its own uniforms (`tMap`, `uOpacity`, `uVelocity`).
  - Vertex shader: pass `uv` through, apply `position` scaled by the mesh scale.
  - Fragment shader: sample `tMap` three times with a per-channel UV offset proportional to `uVelocity` (RGB split) plus a mild sinusoidal UV warp; multiply the result by `uOpacity`. This is the fluid-distortion look.
- `setImages(urls)`: for each URL, `createImageBitmap(await (await fetch(url)).blob())` so decoding stays off the main thread, then upload into a `Texture`. Keep at most `opts.maxTextures` textures, evicting least-recently-used. A single failed URL is skipped (logged at debug level) and must not reject the whole call.
- `render(items, pointer)`: assign one pooled mesh per live `TrailItem` — position from the item's x/y converted to clip space, scale and `uOpacity` from item age, `uVelocity` from the frame's pointer delta, and a `parallaxStrength`-scaled offset. Hide surplus meshes by setting `uOpacity` to 0. One `renderer.render({ scene, camera })` call per frame.
- `resize()`: `renderer.setSize(canvas.clientWidth, canvas.clientHeight)` plus camera perspective update.
- `destroy()`: release textures/bitmaps, drop mesh references, and remove any internally registered listeners.

- [ ] **Step 3: Build the canvas component with the degradation switch**

Create `admin-web/src/components/showcase/ShowcaseCanvas.tsx` (`"use client"`). It owns the decision and must implement **all four** contract rules:

```tsx
// Decided once on mount — never mid-session, which would flicker.
const [useWebGL] = useState(
  () => typeof window !== "undefined" && motionConfig.shouldAnimate() && !config.minimal,
);
const [fellBack, setFellBack] = useState(false);
```

- `motionConfig.shouldAnimate()` returns false under both reduced-motion and low-end (`hardwareConcurrency <= 4`), covering contract rules 1 and 2 in one check.
- When `useWebGL` is false, render `<ShowcaseTrailDOM items={items} config={config} />` and **never call `createShowcaseRenderer`** — so `ogl` is never fetched. Task 7's perf gate asserts exactly this.
- In the mount effect: `createShowcaseRenderer(canvasRef.current, { maxTextures: config.trailMax * 2, parallaxStrength: config.parallaxStrength })`. On rejection → `setFellBack(true)`. Register `canvas.addEventListener("webglcontextlost", (e) => { e.preventDefault(); setFellBack(true); })` before the first frame.
- When `fellBack` is true, render `<ShowcaseTrailDOM ... />` and render no error UI — the degradation is silent by design.
- rAF loop: skip rendering while `document.hidden`; drive `createTrail(...)` from `pointermove` exactly as the DOM version does.
- Cleanup: cancel rAF, `renderer.destroy()`, remove `pointermove` / `visibilitychange` / `resize` / `webglcontextlost` listeners.

- [ ] **Step 4: Switch the page to the canvas**

In `admin-web/src/app/page.tsx`, replace `<ShowcaseTrailDOM items={items} config={config} />` with `<ShowcaseCanvas items={items} config={config} />`. The canvas component renders the DOM trail itself whenever it degrades, so the page holds only one branch.

- [ ] **Step 5: Verify bundle isolation**

Run: `cd admin-web && npx tsc --noEmit && npm run build`
Expected: green, and "First Load JS shared by all" **unchanged** from the previous build.

Then confirm `ogl` is isolated in its own lazy chunk:

```bash
cd admin-web && grep -rl "ogl" .next/static/chunks/*.js
```
Take the basename of the matched chunk and assert no other route pulls it:
```bash
curl -s http://127.0.0.1:18790/admin/works | grep -c "<that-chunk-basename>"
```
Expected: `0`. (The chunk filename is content-hashed — look it up here rather than assuming a name.)

- [ ] **Step 6: Deploy and commit**

```bash
docker compose build --build-arg CACHEBUST="$(date +%s)" admin-web && docker compose up -d --force-recreate admin-web
git add admin-web/package.json admin-web/package-lock.json admin-web/src/lib/showcase/webgl.ts admin-web/src/components/showcase/ShowcaseCanvas.tsx admin-web/src/app/page.tsx
git commit -m "feat(showcase): WebGL trail layer with DOM degradation contract

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Universal fullscreen slideshow

**Files:**
- Create: `admin-web/src/components/SlideshowPlayer.tsx`
- Create: `admin-web/src/lib/useSlideshow.tsx`
- Modify: `admin-web/src/components/index.ts`
- Modify: `admin-web/src/app/admin/creators/[id]/page.tsx`, `admin-web/src/app/admin/works/page.tsx`, `admin-web/src/app/admin/tags/[id]/page.tsx`
- Modify: `admin-web/src/app/globals.css`
- Modify: `admin-web/src/lib/i18n.tsx`

**Interfaces:**
- Consumes: `useShowcaseConfig()` for `slideDwellMs` / `slideTransition` / `slideLoop` / `slideShowMeta`; `usePresence` and `motionTokens` from `@/lib/motion`; the existing signed-media URL helper used by `AssetImage` for `preview` size.
- Produces:
  ```ts
  export interface SlideItem {
    assetId: string;
    workId: string;
    title?: string | null;
    creatorName?: string | null;
  }
  export function useSlideshow(): {
    open: (items: SlideItem[], startIndex?: number) => void;
    node: ReactNode;
  }
  ```

- [ ] **Step 1: Add the Ken Burns keyframes**

In `admin-web/src/app/globals.css`, beside the existing keyframes:

```css
@keyframes kenBurns {
  from { transform: scale(1) translate3d(0, 0, 0); }
  to   { transform: scale(1.08) translate3d(-1.5%, -1%, 0); }
}
.slide-kenburns { animation: kenBurns var(--slide-dwell, 5000ms) ease-out forwards; }
```

Transform-only, per the motion red lines; the existing global `prefers-reduced-motion` kill-switch collapses it automatically.

- [ ] **Step 2: Build the player**

Create `admin-web/src/components/SlideshowPlayer.tsx` (`"use client"`):

- Props: `{ items: SlideItem[]; startIndex: number; open: boolean; onClose: () => void }`
- Mount/unmount through `usePresence(open, motionTokens.duration.base)`; reuse the backdrop enter/exit classes the existing Modal uses so reduced-motion behavior matches.
- Layout: `fixed inset-0 z-[60] bg-black`, the current image centered with `object-contain max-h-screen max-w-full`.
- Two stacked `<img>` layers for crossfade — the outgoing layer fades out while the incoming fades in. With `slideTransition === "kenburns"`, the active layer additionally gets `.slide-kenburns` and inline `style={{ "--slide-dwell": `${config.slideDwellMs}ms` }}`.
- Autoplay: `setInterval`-free `setTimeout` chain of `config.slideDwellMs`, cleared on index change, pause, and unmount.
- Keyboard: `←`/`→` step, `Space` toggles pause, `Esc` closes. Attach on the dialog container, `preventDefault()` on Space so the page doesn't scroll.
- End behavior: with `config.slideLoop` wrap to 0; without it, call `onClose()`.
- Metadata overlay (title, creator, `index + 1 / total`) rendered only when `config.slideShowMeta`.
- A11y: `role="dialog" aria-modal="true"`, focus moved to the container on open and returned to the trigger on close, `aria-label` on every control (prev/next/play-pause/close) — no generic `"Toggle"` labels.
- Images resolve via the existing preview-size media URL helper used by `AssetImage`; do not hand-build `/media/preview/...` strings.

- [ ] **Step 3: Build the launcher hook**

Create `admin-web/src/lib/useSlideshow.tsx` (`.tsx` — it returns JSX):

```tsx
"use client";
import { useCallback, useState, type ReactNode } from "react";
import { SlideshowPlayer, type SlideItem } from "@/components/SlideshowPlayer";

export function useSlideshow(): { open: (items: SlideItem[], startIndex?: number) => void; node: ReactNode } {
  const [state, setState] = useState<{ items: SlideItem[]; startIndex: number; open: boolean }>({
    items: [],
    startIndex: 0,
    open: false,
  });

  const open = useCallback((items: SlideItem[], startIndex = 0) => {
    setState({ items, startIndex, open: true });
  }, []);

  const node = (
    <SlideshowPlayer
      items={state.items}
      startIndex={state.startIndex}
      open={state.open}
      onClose={() => setState((s) => ({ ...s, open: false }))}
    />
  );

  return { open, node };
}
```

Export `SlideshowPlayer` and `SlideItem` from `admin-web/src/components/index.ts` alongside the other component exports.

- [ ] **Step 4: Wire the three entry points**

Each page: `const slideshow = useSlideshow();`, a `btn-ghost` button labelled `t("slideshow.open")` (hidden when the list is empty), and `{slideshow.node}` at the end of the JSX. Map each list item to `SlideItem` using its `thumbnail_asset_id` as `assetId`, its `id` as `workId`, plus `title` and `creator_name`; skip entries without a thumbnail asset.

- `admin-web/src/app/admin/creators/[id]/page.tsx` — button in the works-section header, items = that creator's loaded works
- `admin-web/src/app/admin/works/page.tsx` — button in the page header, items = the current page's `items`
- `admin-web/src/app/admin/tags/[id]/page.tsx` — button in the page header, items = the tag's loaded works

- [ ] **Step 5: Add i18n keys (zh AND en)**

`slideshow.open`, `slideshow.play`, `slideshow.pause`, `slideshow.prev`, `slideshow.next`, `slideshow.close`, `slideshow.counter`

- [ ] **Step 6: Verify, deploy, commit**

Run: `cd admin-web && npx tsc --noEmit && npm run build`
Expected: green.

Manual on `http://127.0.0.1:18790`: launch from a creator page — ←/→ step, Space pauses and resumes, Esc closes, autoplay advances at the configured dwell, `slideLoop: false` ends by closing, and the metadata overlay follows `slideShowMeta`.

```bash
docker compose build --build-arg CACHEBUST="$(date +%s)" admin-web && docker compose up -d --force-recreate admin-web
git add admin-web/src/components/SlideshowPlayer.tsx admin-web/src/lib/useSlideshow.tsx admin-web/src/components/index.ts admin-web/src/app/globals.css "admin-web/src/app/admin/creators/[id]/page.tsx" admin-web/src/app/admin/works/page.tsx "admin-web/src/app/admin/tags/[id]/page.tsx" admin-web/src/lib/i18n.tsx
git commit -m "feat(admin-web): universal fullscreen slideshow with three entry points

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: Performance + accessibility acceptance

**Files:**
- Modify: `docs/frontend-motion-audit.md`
- Modify: `.superpowers/sdd/progress.md`

**Interfaces:**
- Consumes: the MU-T2 recorder (`perf-trace.js`) — logs in or accepts `AG_TOKEN`, traces each page with `PerformanceObserver` for `longtask` / `layout-shift` / LCP, and reports `document.getAnimations()` running count plus the computed `.page-transition` duration, under both `no-preference` and `reduce`. Recreate it from the copy in `docs/frontend-motion-audit.md`'s appendix if the scratchpad was cleaned.

- [ ] **Step 1: Point the recorder at the showcase route**

Set `PAGES = ["/", "/admin/works", "/admin/jobs"]` and `AG_BASE=http://127.0.0.1:18790`. Add a network log to the recorder so the ogl assertion is possible — inside `tracePage`, before `page.goto`:

```js
const requests = [];
page.on("request", (r) => requests.push(r.url()));
```
and attach `data.requests = requests;` beside `data.consoleErrors`.

Mint a token without needing the (rotated) admin password — the credential is never printed:

```bash
TOKEN=$(docker compose exec -T backend python -c "
import asyncio
from sqlalchemy import select
from app.database import async_session
from app.models.user import User
from app.auth import create_access_token
async def main():
    async with async_session() as s:
        u = (await s.execute(select(User).where(User.is_admin==True, User.is_active==True).limit(1))).scalars().first()
        print(create_access_token(u.username, must_change_password=False))
asyncio.run(main())
" 2>/dev/null | tr -d '\r\n')
```

- [ ] **Step 2: Run the recording**

Run the recorder with `AG_TOKEN="$TOKEN"`, `AG_BASE`, and `CHROME_BIN=$HOME/.cache/ms-playwright/chromium-1232/chrome-linux64/chrome`.

**Pass criteria — all four must hold:**
1. `/` long tasks == **0** in both modes.
2. Under `reduce` on `/`: `runningAnimations == 0` **and** no entry in `data.requests` matches the ogl chunk basename found in Task 5 Step 5.
3. `/` CLS **< 0.1** — dimensions ship with the payload, so there is no excuse for shift.
4. `/admin/works` and `/admin/jobs` are no worse than the 2026-07-20 baseline in the audit appendix (long tasks still 0).

If criterion 1 or 3 fails, fix it before proceeding. Do not record a passing note over a failing run.

- [ ] **Step 3: Write the appendix and ledger**

Append a 展示页 subsection to the existing 附录 in `docs/frontend-motion-audit.md`, using the same table columns as the MU-T2 table (页面 / 模式 / FCP / LCP / CLS / long tasks / 运行中动画), plus one line stating whether the ogl chunk was requested under `reduce`.

Append to `.superpowers/sdd/progress.md`:

```
# ── Phase: 展示页首页 + 通用幻灯片 (2026-07-20, frontend-motion) ──
Spec docs/superpowers/specs/2026-07-20-showcase-homepage-design.md; plan docs/superpowers/plans/2026-07-20-showcase-homepage.md.
T1 取样端点(随机窗口,非 ORDER BY random)+ 签名 preview URL;T2 showcase 偏好键(含 preferencesSync 整体替换补键);T3 设置页四组;T4 DOM 版展示页 + landing 路由;T5 WebGL 层 + 四条降级契约;T6 通用幻灯片三入口;T7 性能验收。
```

- [ ] **Step 4: Commit**

```bash
git add docs/frontend-motion-audit.md .superpowers/sdd/progress.md
git commit -m "docs(showcase): perf/a11y acceptance recording and ledger

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:** ①取样端点 → T1;②WebGL 展示页 → T4(DOM 骨架)+ T5(WebGL 层);③通用幻灯片 → T6;④设置子页 → T2(存储管道)+ T3(界面);数据流(spec §4)→ T4 Step 5;错误处理(spec §5)→ T4 Step 5(空/错)、T5 Step 3(降级静默)、T5 Step 2(单图失败跳过)、T1(签名);测试与验收(spec §6)→ T1 pytest、各任务 build、T7 录制;风险 S1–S6 全部落到具体步骤(S2 → T5 Step 5 的 bundle 核验,S4 → 签名 TTL 与整批重取,S5 → T4 Step 6 landing 偏好)。
- **Discovered-fact corrections vs spec:** `WorkList` 不含 width/height(尺寸在 `Asset` 上)→ T1 用一次 `Asset.id.in_(...)` 批量取维度;`PUT /me/preferences` 是**整体替换**而非合并 → T2 Step 6 必须同步给 `preferencesSync` 补键,否则展示配置会被下次改主题静默抹掉。
- **Type consistency:** `ShowcaseItem` 字段在 T1(Pydantic)与 T4(TS)逐字段一致;`TrailItem`/`TrailController`/`createTrail` 在 T4 定义、T5 消费;`ShowcaseConfig` 在 T2 定义,T3/T4/T5/T6 均按同名字段读取;`SlideItem`/`useSlideshow` 在 T6 内自洽并由三处入口消费。
- **Open verification point:** T5 Step 5 的 ogl chunk 文件名是内容哈希,写成"构建后现查 basename 再断言",T7 的 ogl 断言复用同一 basename。
