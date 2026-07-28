# Frontend Optimization — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate ~329 hardcoded semantic colors to palette tokens (zero GitHub-theme regression), tighten polling and thumbnail caching, and unify the bell with the server task feed.

**Architecture:** Three independent tracks — A: color-utility → token migration + a `Banner` primitive, done in per-file batches; B: a shared polling helper + a backend `Cache-Control` header for thumbnails; C: point the notification bell's list at the server `task_runs` feed. No data-model changes.

**Tech Stack:** Next.js 14 + React 18 + TypeScript + Tailwind (admin-web); FastAPI (backend, one handler + one pytest).

## Global Constraints

- **Zero visual regression in the GitHub default theme.** Map each hardcoded color to the token whose GitHub value is the same hue; preserve each element's fill/outline shape (do NOT reshape buttons into `.btn-*`).
- **Exclude per-source / brand colors** — never touch `admin-web/src/lib/sourceColors.ts` or per-source badge colors (constraint: `source-colors`).
- **Collapse `X dark:Y` pairs into the single token** (tokens auto-flip in dark mode; drop the `dark:` color variant).
- Comments in code: English only.
- Frontend build check: `cd <repo-root>/admin-web && npm run build` (must be green).
- Backend test: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest <args>`.
- End every commit message with: `Co-Authored-By: Claude <noreply@anthropic.com>`.

## Migration Procedure (Track A — shared reference for Tasks 4–8)

Apply this mapping to every in-scope occurrence in the task's files. When a class
has a `dark:` color sibling (e.g. `text-red-600 dark:text-red-400`), replace the
**pair** with the single token class.

| Hardcoded (incl. `dark:` pair) | → Token class |
|---|---|
| `text-blue-300..700` | `text-accent` |
| `bg-blue-600/700` (+`text-white`, filled button) | `bg-accent text-white hover:bg-accent/90` |
| `bg-blue-50` / `bg-blue-900/..` | `bg-accent-subtle` |
| `bg-blue-100 text-blue-700` (avatar) | `bg-accent-subtle text-accent` |
| `border-blue-200/..` | `border-accent/30` |
| `text-red-400..700` | `text-danger` |
| `bg-red-600` (+`text-white`, filled) | `bg-danger text-white hover:bg-danger/90` |
| `bg-red-50` / `bg-red-900/..` | `bg-danger-subtle` |
| `border-red-200/800` | `border-danger/30` |
| `text-green-400..700` | `text-success` |
| `bg-green-50` / `bg-green-900/..` | `bg-success-subtle` |
| `text-yellow-600` / `text-amber-*` | `text-warning` |
| yellow/amber banner bg + border | `bg-warning-subtle` + `border-warning/30` |
| residual `slate/gray/zinc/neutral` text | `text-muted` (secondary) / `text-fg` (primary) |
| residual gray bg / border | `bg-subtle` / `border-border` |

Per-batch verification (run from `admin-web/`), excluding source colors:

```bash
# Should print 0 (no in-scope hardcoded semantic colors remain in the batch files):
grep -hoE "(bg|text|border)-(blue|red|green|yellow|amber)-[0-9]{2,3}" <files...> | wc -l
```

Where a file has a repeated inline tinted-banner box, replace it with the `<Banner>` component from Task 3.

---

### Task 1: Backend — `Cache-Control` on `/media/thumb`

**Files:**
- Modify: `backend/app/api/media.py` (the `thumb` handler, ~line 63-65)
- Test: `backend/tests/test_media_cache.py` (create)

**Interfaces:**
- Consumes: existing `_serve(asset_id: str, size: str) -> FileResponse`.
- Produces: `/media/thumb` responses carry `Cache-Control: public, max-age=86400`; `/media/preview` and `/media/original` are unchanged (no caching).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_media_cache.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_thumb_sets_cache_control(tmp_path, monkeypatch):
    from fastapi.responses import FileResponse
    from app.api import media

    f = tmp_path / "t.webp"
    f.write_bytes(b"fake-webp")

    async def fake_serve(asset_id, size):
        assert size == "thumb"
        return FileResponse(str(f), media_type="image/webp")

    monkeypatch.setattr(media, "_serve", fake_serve)

    resp = await media.thumb("any-asset-id")
    assert resp.headers["cache-control"] == "public, max-age=86400"


@pytest.mark.asyncio
async def test_serve_does_not_add_cache_control():
    # Guards that preview/original (which call _serve directly) stay uncached.
    from app.api import media
    import inspect

    src = inspect.getsource(media._serve)
    assert "Cache-Control" not in src and "cache-control" not in src
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest tests/test_media_cache.py -v`
Expected: FAIL — `test_thumb_sets_cache_control` raises `KeyError: 'cache-control'`.

- [ ] **Step 3: Implement**

In `backend/app/api/media.py`, replace the `thumb` handler:

```python
@router.get("/media/thumb/{asset_id}")
async def thumb(asset_id: str):
    """Serve thumbnail — no auth needed (embedded in <img> tags on admin-web)."""
    return await _serve(asset_id, "thumb")
```

with:

```python
@router.get("/media/thumb/{asset_id}")
async def thumb(asset_id: str):
    """Serve thumbnail — no auth needed (embedded in <img> tags on admin-web).

    Thumbnails are content-addressed by asset id and never change, so they are
    safe to cache in the browser. preview/original stay uncached (auth-gated).
    """
    resp = await _serve(asset_id, "thumb")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest tests/test_media_cache.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/media.py backend/tests/test_media_cache.py
git commit -m "perf(media): cache thumbnails (Cache-Control on /media/thumb)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Frontend — polling helper + thumbnail `decoding`

**Files:**
- Create: `admin-web/src/lib/polling.ts`
- Modify: `admin-web/src/app/admin/scheduler/page.tsx` (the `ops` query, ~line 76-80)
- Modify: `admin-web/src/app/admin/settings/logs/page.tsx` (~line 29)
- Modify: `admin-web/src/app/admin/jobs/page.tsx` (the local `REFETCH_ACTIVE_MS`/`REFETCH_IDLE_MS` consts, lines 15-16)
- Modify (decoding): `admin-web/src/app/admin/tags/[id]/page.tsx`, `creators/[id]/page.tsx`, `repositories/[id]/page.tsx` (2 imgs), `curation/page.tsx`, `search/page.tsx`, `page.tsx` (dashboard)

**Interfaces:**
- Produces: `POLL_ACTIVE_MS = 8000`, `POLL_IDLE_MS = 30000`, `pollInterval(active: boolean): number`, `hasActiveTask(items?: { status?: string | null }[] | null): boolean` exported from `@/lib/polling`.

- [ ] **Step 1: Create `admin-web/src/lib/polling.ts`**

```ts
// Shared polling cadence so background refetch load stays predictable.
// Active = something is in flight (poll fast); idle = poll slowly.
export const POLL_ACTIVE_MS = 8000;
export const POLL_IDLE_MS = 30000;

export function pollInterval(active: boolean): number {
  return active ? POLL_ACTIVE_MS : POLL_IDLE_MS;
}

const NONTERMINAL = new Set([
  "enqueued", "running", "paused", "recovering",
  "downloading", "downloaded", "importing",
]);

export function hasActiveTask(items?: { status?: string | null }[] | null): boolean {
  return !!items?.some((t) => t.status != null && NONTERMINAL.has(t.status));
}
```

- [ ] **Step 2: Fix the scheduler poller (2s → adaptive)**

In `admin-web/src/app/admin/scheduler/page.tsx`, add the import near the other `@/lib` imports:

```tsx
import { pollInterval, hasActiveTask } from "@/lib/polling";
```

Replace the `ops` query's `refetchInterval: 2000,` with:

```tsx
    refetchInterval: (query) => pollInterval(hasActiveTask((query.state.data as { items?: { status?: string | null }[] } | undefined)?.items)),
```

- [ ] **Step 3: Fix the logs poller**

In `admin-web/src/app/admin/settings/logs/page.tsx`, add:

```tsx
import { POLL_ACTIVE_MS } from "@/lib/polling";
```

Replace `refetchInterval: autoRefresh ? 5000 : undefined,` with:

```tsx
    refetchInterval: autoRefresh ? POLL_ACTIVE_MS : false,
```

- [ ] **Step 4: Centralize the jobs-page constants (no behavior change)**

In `admin-web/src/app/admin/jobs/page.tsx`, delete the two local lines:

```tsx
const REFETCH_ACTIVE_MS = 8000;
const REFETCH_IDLE_MS = 30000;
```

and add an import (with the other `@/lib` imports):

```tsx
import { POLL_ACTIVE_MS as REFETCH_ACTIVE_MS, POLL_IDLE_MS as REFETCH_IDLE_MS } from "@/lib/polling";
```

(The aliased import keeps every existing reference in the file working unchanged.)

- [ ] **Step 5: Add `decoding="async"` to the raw thumbnail imgs**

In each of these `<img ... loading="lazy" />` tags, add `decoding="async"` right after `loading="lazy"`:
- `admin-web/src/app/admin/tags/[id]/page.tsx`
- `admin-web/src/app/admin/creators/[id]/page.tsx`
- `admin-web/src/app/admin/repositories/[id]/page.tsx` (both imgs)
- `admin-web/src/app/admin/curation/page.tsx`
- `admin-web/src/app/admin/search/page.tsx`
- `admin-web/src/app/admin/page.tsx`

Example (search page): `... loading="lazy" />` → `... loading="lazy" decoding="async" />`.

- [ ] **Step 6: Build to verify**

Run: `cd <repo-root>/admin-web && npm run build`
Expected: green build, no TypeScript errors.

- [ ] **Step 7: Commit**

```bash
git add admin-web/src/lib/polling.ts admin-web/src/app/admin/scheduler/page.tsx admin-web/src/app/admin/settings/logs/page.tsx admin-web/src/app/admin/jobs/page.tsx admin-web/src/app/admin/tags/ admin-web/src/app/admin/creators/ admin-web/src/app/admin/repositories/ admin-web/src/app/admin/curation/page.tsx admin-web/src/app/admin/search/page.tsx admin-web/src/app/admin/page.tsx
git commit -m "perf(admin-web): shared adaptive polling + thumbnail decoding=async

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Frontend — `Banner` primitive

**Files:**
- Create: `admin-web/src/components/Banner.tsx`
- Modify: `admin-web/src/components/index.ts`

**Interfaces:**
- Produces: `Banner` (default export), props `{ tone?: "info" | "success" | "warning" | "danger"; title?: string; children?: ReactNode; className?: string }`. Used by Tasks 4–8 to replace inline tinted-banner boxes.

- [ ] **Step 1: Create `admin-web/src/components/Banner.tsx`**

```tsx
import type { ReactNode } from "react";

type Tone = "info" | "success" | "warning" | "danger";

// Token-based tinted banner. Reskins with the active palette automatically.
const toneClasses: Record<Tone, string> = {
  info: "border-accent/30 bg-accent-subtle text-accent",
  success: "border-success/30 bg-success-subtle text-success",
  warning: "border-warning/30 bg-warning-subtle text-warning",
  danger: "border-danger/30 bg-danger-subtle text-danger",
};

export default function Banner({
  tone = "info",
  title,
  children,
  className = "",
}: {
  tone?: Tone;
  title?: string;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <div className={`rounded-md border px-3 py-2 text-sm ${toneClasses[tone]} ${className}`} role="status">
      {title && <div className="font-medium">{title}</div>}
      {children != null && <div className={title ? "mt-0.5" : ""}>{children}</div>}
    </div>
  );
}
```

- [ ] **Step 2: Export it from `admin-web/src/components/index.ts`**

Add:

```tsx
export { default as Banner } from "./Banner";
```

- [ ] **Step 3: Build to verify**

Run: `cd <repo-root>/admin-web && npm run build`
Expected: green build.

- [ ] **Step 4: Commit**

```bash
git add admin-web/src/components/Banner.tsx admin-web/src/components/index.ts
git commit -m "feat(admin-web): Banner primitive (token-based tinted alert)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Track A batch 1 — `reference/danbooru/page.tsx`

**Files:**
- Modify: `admin-web/src/app/admin/reference/danbooru/page.tsx` (96 occurrences)

- [ ] **Step 1: Apply the Migration Procedure** (mapping table at top of this plan) to the file. Collapse `dark:` color pairs. Replace inline tinted-banner boxes with `<Banner tone=...>` (import `Banner` from `@/components`). Do not touch any per-source color (none expected here).

- [ ] **Step 2: Verify no in-scope residue**

Run:
```bash
cd <repo-root>/admin-web
grep -hoE "(bg|text|border)-(blue|red|green|yellow|amber)-[0-9]{2,3}" src/app/admin/reference/danbooru/page.tsx | wc -l
```
Expected: `0`.

- [ ] **Step 3: Build**

Run: `cd <repo-root>/admin-web && npm run build`
Expected: green.

- [ ] **Step 4: Visual spot-check** — open `/admin/reference/danbooru`, confirm it looks unchanged in the GitHub theme, then toggle the palette to Nord and confirm badges/banners/buttons reskin (no fixed blue/red/green).

- [ ] **Step 5: Commit**

```bash
git add admin-web/src/app/admin/reference/danbooru/page.tsx
git commit -m "style(admin-web): tokenize danbooru reference page colors

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Track A batch 2 — data management pages

**Files:**
- Modify: `admin-web/src/app/admin/data-mgmt/page.tsx` (76)
- Modify: `admin-web/src/app/admin/settings/data-mgmt/page.tsx` (22)

- [ ] **Step 1:** Apply the Migration Procedure to both files; replace inline tinted-banner boxes with `<Banner>`.
- [ ] **Step 2: Verify residue = 0**
```bash
cd <repo-root>/admin-web
grep -hoE "(bg|text|border)-(blue|red|green|yellow|amber)-[0-9]{2,3}" src/app/admin/data-mgmt/page.tsx src/app/admin/settings/data-mgmt/page.tsx | wc -l
```
Expected: `0`.
- [ ] **Step 3: Build** — `cd <repo-root>/admin-web && npm run build` → green.
- [ ] **Step 4: Visual spot-check** — `/admin/data-mgmt` + settings/data-mgmt, GitHub unchanged + Nord reskins.
- [ ] **Step 5: Commit**
```bash
git add admin-web/src/app/admin/data-mgmt/page.tsx admin-web/src/app/admin/settings/data-mgmt/page.tsx
git commit -m "style(admin-web): tokenize data-management page colors

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Track A batch 3 — jobs + NotificationCenter

**Files:**
- Modify: `admin-web/src/app/admin/jobs/page.tsx` (34)
- Modify: `admin-web/src/components/NotificationCenter.tsx` (13)

- [ ] **Step 1:** Apply the Migration Procedure to both files. NotificationCenter uses `bg-blue-500`/`text-red-400`/`text-green-400` for status dots/text — map dot `bg-blue-500` → `bg-accent`, `text-green-400` → `text-success`, `text-red-400` → `text-danger`. Replace inline banners with `<Banner>` if any.
- [ ] **Step 2: Verify residue = 0**
```bash
cd <repo-root>/admin-web
grep -hoE "(bg|text|border)-(blue|red|green|yellow|amber)-[0-9]{2,3}" src/app/admin/jobs/page.tsx src/components/NotificationCenter.tsx | wc -l
```
Expected: `0`.
- [ ] **Step 3: Build** → green.
- [ ] **Step 4: Visual spot-check** — `/admin/jobs` rows + bell dropdown, GitHub unchanged + Nord reskins.
- [ ] **Step 5: Commit**
```bash
git add admin-web/src/app/admin/jobs/page.tsx admin-web/src/components/NotificationCenter.tsx
git commit -m "style(admin-web): tokenize jobs page + notification center colors

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: Track A batch 4 — settings group

**Files:**
- Modify: `admin-web/src/app/admin/settings/backup/page.tsx` (25)
- Modify: `admin-web/src/app/admin/settings/proxy/page.tsx` (15)
- Modify: `admin-web/src/app/admin/settings/profile/page.tsx` (13)
- Modify: `admin-web/src/app/admin/settings/auth-status/page.tsx` (13)
- Modify: `admin-web/src/app/admin/settings/logs/page.tsx` (10)
- Modify: `admin-web/src/app/admin/settings/gallerydl/page.tsx` (9)
- Modify: `admin-web/src/app/admin/settings/download-defaults/page.tsx` (9)

- [ ] **Step 1:** Apply the Migration Procedure to all seven files. The profile page's amber must-change banner (`bg-amber-50 border-amber-300 text-amber-800 ...`) becomes `<Banner tone="warning" title={t("auth.force_change_title")}>{t("auth.force_change_message")}</Banner>`. The blue avatar block (`bg-blue-100 text-blue-700`) → `bg-accent-subtle text-accent`.
- [ ] **Step 2: Verify residue = 0**
```bash
cd <repo-root>/admin-web
grep -hoE "(bg|text|border)-(blue|red|green|yellow|amber)-[0-9]{2,3}" src/app/admin/settings/backup/page.tsx src/app/admin/settings/proxy/page.tsx src/app/admin/settings/profile/page.tsx src/app/admin/settings/auth-status/page.tsx src/app/admin/settings/logs/page.tsx src/app/admin/settings/gallerydl/page.tsx src/app/admin/settings/download-defaults/page.tsx | wc -l
```
Expected: `0`.
- [ ] **Step 3: Build** → green.
- [ ] **Step 4: Visual spot-check** — each settings page, GitHub unchanged + Nord reskins.
- [ ] **Step 5: Commit**
```bash
git add admin-web/src/app/admin/settings/backup/page.tsx admin-web/src/app/admin/settings/proxy/page.tsx admin-web/src/app/admin/settings/profile/page.tsx admin-web/src/app/admin/settings/auth-status/page.tsx admin-web/src/app/admin/settings/logs/page.tsx admin-web/src/app/admin/settings/gallerydl/page.tsx admin-web/src/app/admin/settings/download-defaults/page.tsx
git commit -m "style(admin-web): tokenize settings pages colors

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: Track A batch 5 — search / mapping / tags

**Files:**
- Modify: `admin-web/src/app/admin/search/page.tsx` (11)
- Modify: `admin-web/src/app/admin/creators/[id]/mapping/page.tsx` (11)
- Modify: `admin-web/src/app/admin/tags/[id]/page.tsx` (8)

- [ ] **Step 1:** Apply the Migration Procedure to all three files; replace inline banners with `<Banner>`.
- [ ] **Step 2: Verify residue = 0**
```bash
cd <repo-root>/admin-web
grep -hoE "(bg|text|border)-(blue|red|green|yellow|amber)-[0-9]{2,3}" src/app/admin/search/page.tsx "src/app/admin/creators/[id]/mapping/page.tsx" "src/app/admin/tags/[id]/page.tsx" | wc -l
```
Expected: `0`.
- [ ] **Step 3: Build** → green.
- [ ] **Step 4: Visual spot-check** — search, creator mapping, tag detail; GitHub unchanged + Nord reskins.
- [ ] **Step 5: Commit**
```bash
git add admin-web/src/app/admin/search/page.tsx "admin-web/src/app/admin/creators/[id]/mapping/page.tsx" "admin-web/src/app/admin/tags/[id]/page.tsx"
git commit -m "style(admin-web): tokenize search + mapping + tag-detail colors

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 9: Track C — bell list reads the server task feed

**Files:**
- Modify: `admin-web/src/components/NotificationCenter.tsx` (the `NotificationBell` component)

**Interfaces:**
- Consumes: `api.listTasks({ limit })` → `TaskRunListResponse`; `queryKeys.tasks.all`.

- [ ] **Step 1: Add a server-backed recent-tasks query in `NotificationBell`**

Inside `NotificationBell`, add (it already imports `useQuery`, `api`, `queryKeys`):

```tsx
  const recent = useQuery({
    queryKey: [...queryKeys.tasks.all, "bell-recent"],
    queryFn: () => api.listTasks({ limit: 10 }),
    enabled: open,           // only fetch when the dropdown is open
    staleTime: 10_000,
  });
```

- [ ] **Step 2: Render the server "recent" section from `recent.data.items`**

Keep the existing in-session `batchJob` / `operationJob` overlays at the top (relabel that group header to `进行中` / "In progress"). Below them, render a `最近` / "Recent" section that maps `recent.data?.items ?? []` to rows (reuse the existing row markup: `statusIcon`, title, `timeAgo(new Date(task.created_at).getTime())`, click → `taskLink`). Replace the previous in-memory `items.map(...)` list with this server list. If `recent.isError`, render nothing for that section (overlays still show).

- [ ] **Step 3: Build to verify**

Run: `cd <repo-root>/admin-web && npm run build`
Expected: green build (note: this file was also touched in Task 6 for colors — that is fine, they are different lines).

- [ ] **Step 4: Visual spot-check** — open the bell; confirm "Recent" shows the same entries as `/admin/notifications` (server-backed), and an in-flight operation still appears live under "In progress".

- [ ] **Step 5: Commit**

```bash
git add admin-web/src/components/NotificationCenter.tsx
git commit -m "feat(admin-web): bell recent list reads server task_runs feed

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Track A token migration (mapping, exclude sourceColors, collapse dark:, batches): Tasks 4–8 + shared Migration Procedure. ✓
- `Banner` primitive: Task 3, used in Tasks 4–8. ✓
- Track B1 polling (shared consts, scheduler/logs fix, jobs centralize): Task 2. ✓
- Track B2 images (lazy already done → decoding + backend Cache-Control): Task 1 (backend) + Task 2 Step 5 (decoding). ✓
- Track C bell ↔ server feed: Task 9. ✓
- Testing (backend Cache-Control pytest; per-batch build + visual; residue grep): Task 1 + every batch. ✓
- Zero-regression / exclude per-source / collapse dark:: Global Constraints + Migration Procedure. ✓
- Out of scope respected (no RSC, no next/image, no confirm(), no sourceColors). ✓

**Type consistency:** `pollInterval`/`hasActiveTask`/`POLL_ACTIVE_MS`/`POLL_IDLE_MS` defined in Task 2 Step 1, consumed in Steps 2–4. `Banner` props defined in Task 3 match usage in Tasks 4–8. `api.listTasks({ limit })` and `queryKeys.tasks.all` consistent with the existing API (used by `/admin/notifications` and scheduler). The jobs-page aliased import preserves `REFETCH_ACTIVE_MS`/`REFETCH_IDLE_MS` identifiers.

**Placeholder scan:** none — backend code is shown in full; the migration tasks reference one concrete shared mapping table + an exact residue-check command per batch rather than vague "fix colors". Task 9 Step 2 describes reusing existing in-file markup (named helpers `statusIcon`/`timeAgo`/`taskLink`) rather than inventing new structure.

**Note for the implementer:** NotificationCenter.tsx is touched twice (Task 6 colors, Task 9 bell list). Execute Task 6 before Task 9 — they edit different regions.
