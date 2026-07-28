# Multi-user Management + Manual Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fine-grained multi-user management (module-toggle permissions, per-user prefs/NSFW/quota, users pages) plus a working Manual Upload provider, and the two leftover motion-plan items (component extraction, browser perf recording).

**Architecture:** Per-request DB permission checks via a `RequirePermission(module)` dependency swapped in at each router's `dependencies=[...]`; users CRUD follows the route→service→repository layering; manual upload reuses the disk-import pipeline (`DownloadJob(status="downloaded")` + `_enqueue_import`) with synthesized metadata JSONs parsed by `ManualProvider`.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async + Alembic; Next.js 14 App Router + TanStack Query; RQ `imports` queue.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-15-multiuser-and-manual-upload-design.md` — module registry, quota/NSFW semantics, ownership model are normative.
- `users.is_active` ALREADY EXISTS (discovered during fact-gathering) — migration adds only: `is_admin, permissions, preferences, nsfw_visible, upload_quota_bytes, upload_used_bytes` (+ `last_login_at`).
- All admin APIs stay JWT-authed; `/media/thumb/` stays open; `shell=True` forbidden; `Path.relative_to` containment for upload paths; never expose NAS paths to clients.
- Frontend: typed API client (`@/lib/api`), i18n keys in BOTH zh/en, thin pages, existing components (`Modal`, `ConfirmDialog`, `PageShell`, `EmptyState`).
- Every task: pytest green → deploy (CACHEBUST rebuild + recreate) → commit with `Co-Authored-By: Claude <noreply@anthropic.com>`.
- Test command: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest <path> -q`.
- EXECUTION PREREQUISITE: Docker Desktop WSL integration must be running (it was down when this plan was written — verify with `docker compose ps` first).

---

### Task 1 (§0a): Extract inline components from jobs and work-detail pages

**Files:**
- Create: `admin-web/src/components/JobDrawers.tsx` (exports `TaskDetailDrawer`, `JobDetailDrawer`)
- Create: `admin-web/src/components/WorkViewerParts.tsx` (exports `FullImageLightbox`, `DisclosurePanel`)
- Modify: `admin-web/src/app/admin/jobs/page.tsx` (delete moved code, import from components)
- Modify: `admin-web/src/app/admin/works/[id]/page.tsx` (same)
- Modify: `admin-web/src/components/index.ts` (barrel exports)

**Interfaces:**
- Produces: `TaskDetailDrawer({id, onClose, onRetryTask, onOpenDownload, onOpenImport})`, `JobDetailDrawer({kind, id, onClose, onRetryDownload, onPauseDownload, onResumeDownload, onDeleteDownload, onRetryImport, onDeleteImport})`, `FullImageLightbox({asset, onClose})`, `DisclosurePanel({storageKey, title, count?, defaultOpen?, children})` — props UNCHANGED from the inline versions.

- [ ] **Step 1:** Copy `TaskDetailDrawer` + `JobDetailDrawer` from `jobs/page.tsx` into `JobDrawers.tsx` verbatim, along with the page-local helpers they reference (grep each identifier inside the moved bodies, e.g. `shortId`, `JsonBlock`; helpers used ONLY by the drawers move with them, helpers also used elsewhere move to `JobDrawers.tsx` and get imported back into the page). Add needed imports (`useQuery`, `queryKeys`, `api`, `usePresence`, `motionTokens`, i18n, types).
- [ ] **Step 2:** Same for `FullImageLightbox` + `DisclosurePanel` (+ `ArrowIcon`, `AssetData` interface) → `WorkViewerParts.tsx`; export `AssetData` type.
- [ ] **Step 3:** Replace inline definitions with imports in both pages; add the two files to `components/index.ts`.
- [ ] **Step 4:** Verify: `cd admin-web && npx tsc --noEmit && npm run build` → green; `git diff --stat` shows only the five files above.
- [ ] **Step 5:** Deploy admin-web (CACHEBUST rebuild + recreate), click-check jobs drawer + work lightbox still animate.
- [ ] **Step 6:** Commit: `refactor(admin-web): extract job drawers and work viewer parts (R1/R2)`.

### Task 2 (§0b): Browser performance recording — BLOCKED on user

- [ ] **Step 1:** Ask user to run `sudo npx playwright install-deps chromium` (in `admin-web/`). DO NOT proceed until confirmed.
- [ ] **Step 2:** Extend the scratchpad `motion-a11y-check.js` into `motion-perf-trace.js`: login via `page.request.post('/api/v1/auth/login')` with credentials sourced from `.env` through the shell environment (`set -a; source .env` — NEVER echo them); store token in `localStorage`; for each of `/admin`, `/admin/works`, `/admin/jobs`: start CDP tracing (`page.context().newCDPSession(page)` → `Tracing.start` with `devtools.timeline`), load + one interaction (paginate works, open a drawer), stop; parse trace events for tasks >50ms and `LayoutShift` records (CLS sum).
- [ ] **Step 3:** Also run the reduced-motion probe from `motion-a11y-check.js` (now that chromium launches).
- [ ] **Step 4:** Append results as `## 附录:性能录制` to `docs/frontend-motion-audit.md`; commit `docs(frontend): browser perf recording appendix`.

### Task 3 (A1): users columns migration + model + UserService

**Files:**
- Modify: `backend/app/models/user.py`
- Create: `backend/alembic/versions/<rev>_add_user_permission_columns.py`
- Create: `backend/app/permissions.py`
- Create: `backend/app/services/users.py` (`UserService`)
- Test: `backend/tests/test_users_service.py`

**Interfaces:**
- Produces: `User.is_admin/permissions/preferences/nsfw_visible/upload_quota_bytes/upload_used_bytes/last_login_at`; `PERMISSION_MODULES: dict[str, str]` with keys `library,curation,upload,subscriptions,tasks,system`; `UserService(db)` with `list()/get(id)/create(username,password,display_name=None,is_admin=False,permissions=[])/update(id, **fields)/delete(id)/reset_password(id)->str`, raising `ValueError` on invalid module names and last-admin violations.

- [ ] **Step 1:** Model columns (append to `User`):

```python
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import BigInteger

    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    permissions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    preferences: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    nsfw_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    upload_quota_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    upload_used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 2:** Migration — run `docker compose exec backend alembic heads` for the current head id; hand-write the revision: `op.add_column("users", ...)` × 7 with matching server_defaults, plus backfill `op.execute("UPDATE users SET is_admin = true")` (every pre-existing user was the admin); symmetric `downgrade`. Verify in container: `alembic upgrade head` → `alembic downgrade -1` → `alembic upgrade head`.
- [ ] **Step 3:** `app/permissions.py`:

```python
PERMISSION_MODULES: dict[str, str] = {
    "library": "图库浏览",
    "curation": "策展操作",
    "upload": "手动上传",
    "subscriptions": "订阅与来源",
    "tasks": "任务中心",
    "system": "系统与设置",
}
```

- [ ] **Step 4:** `UserService` (async; queries inline, user volume tiny): `create` hashes via `app.auth.hash_password`, sets `must_change_password=True`, validates username uniqueness (raise `ValueError("username taken")`); `update` whitelists `display_name,is_active,is_admin,permissions,nsfw_visible,upload_quota_bytes,upload_used_bytes`, validates `set(permissions) <= set(PERMISSION_MODULES)`; `delete` / `update(is_admin=False)` / `update(is_active=False)` raise `ValueError("last admin")` when the target is the only remaining active admin; `reset_password` returns `secrets.token_urlsafe(9)` plaintext once, stores hash, flips `must_change_password`.
- [ ] **Step 5:** Tests (session fixture pattern from existing `tests/`, e.g. `test_users_service.py` seeds via `async_session`): create→list contains→update permissions→invalid module raises→last-admin guard raises on demote/disable/delete→reset_password ≥12 chars + flag flipped. Run targeted → PASS.
- [ ] **Step 6:** Full suite + deploy backend + commit `feat(users): permission columns, module registry, UserService`.

### Task 4 (A2): RequirePermission + users API + /me

**Files:**
- Modify: `backend/app/auth.py` (add `_load_active_user`, `RequirePermission`, `RequireAdminUser`)
- Create: `backend/app/api/users.py`, `backend/app/schemas/users.py`
- Modify: `backend/app/api/auth_api.py` (`/me` payload + `PUT /me/preferences` + set `last_login_at` in `login`), `backend/app/api/__init__.py` (register `/users`)
- Test: `backend/tests/test_permissions_dep.py`, `backend/tests/test_users_api.py`

**Interfaces:**
- Produces: `RequirePermission(module: str)` — 401 invalid token or inactive user; pass-through returning the `User` row for admins and module holders; 403 otherwise. `RequireAdminUser` — like above but requires `is_admin`. `GET /api/v1/me` → `{username, display_name, is_admin, permissions, modules, preferences, nsfw_visible, upload_quota_bytes, upload_used_bytes}`. Users CRUD under `/api/v1/users`.

- [ ] **Step 1:** In `app/auth.py`:

```python
async def _load_active_user(username: str):
    from sqlalchemy import select
    from app.database import async_session
    from app.models.user import User
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid or missing credentials")
    return user


def RequirePermission(module: str):
    async def _check(
        request: Request,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    ):
        username = await get_admin_key(request, credentials)  # existing 401 + pwd-change logic
        user = await _load_active_user(username)
        if user.is_admin or module in (user.permissions or []):
            return user
        raise HTTPException(status_code=403, detail=f"Missing permission: {module}")
    return Depends(_check)


async def _admin_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    username = await get_admin_key(request, credentials)
    user = await _load_active_user(username)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return user

RequireAdminUser = Depends(_admin_user)
```

  `RequireAdmin` (any authenticated user) keeps its current semantics until Task 6 swaps router mounts.
- [ ] **Step 2:** `schemas/users.py`: `UserOut(id,username,display_name,is_admin,is_active,permissions,nsfw_visible,upload_quota_bytes,upload_used_bytes,must_change_password,last_login_at,created_at)`, `UserCreate(username,password,display_name=None,is_admin=False,permissions=[])`, `UserUpdate` (all optional), `ResetPasswordOut(password)`, `MeOut(...)+modules: dict[str,str]`, `PreferencesIn(preferences: dict)` — key whitelist `{"theme","palette","lang","appearance"}` enforced with 400.
- [ ] **Step 3:** `api/users.py`: `router = APIRouter(dependencies=[])` with per-route `admin: User = RequireAdminUser` (needed to compare "self" on delete): GET `""` list, POST create (ValueError→409 for dup / 400 otherwise), GET/PATCH/DELETE `/{user_id}` (delete self → 400), POST `/{user_id}/reset-password` → `ResetPasswordOut`. Register: `api_router.include_router(users_router, prefix="/users", tags=["users"])`.
- [ ] **Step 4:** `auth_api.py`: extend `/me` to MeOut (+`modules=PERMISSION_MODULES`); add `PUT /me/preferences`; in `login` after successful verify: `user.last_login_at = func.now()` and commit.
- [ ] **Step 5:** Tests. `test_permissions_dep.py`: seed admin/module-user/inactive/no-module users; probe app route mounting `RequirePermission("library")` → 200/200/401/403. `test_users_api.py`: CRUD + guards (dup 409, delete self 400, demote last admin 400, reset password round-trip logs in with new password) via the existing httpx AsyncClient app fixture pattern (see `tests/test_gitllery_api.py`).
- [ ] **Step 6:** Full suite + deploy + commit `feat(users): RequirePermission dependency, users CRUD API, /me profile`.

### Task 5 (A3): frontend users pages

**Files:**
- Create: `admin-web/src/app/admin/users/page.tsx`, `admin-web/src/app/admin/users/[id]/page.tsx`
- Modify: `admin-web/src/lib/api/index.ts` + `types.ts` (users endpoints, `Me` type, `queryKeys.users`, `queryKeys.me`), `admin-web/src/app/admin/layout.tsx` (nav entry, admin-only), `admin-web/src/lib/i18n.tsx` (zh+en `users.*`, `nav.users`)

**Interfaces:**
- Consumes: Task 4 API. Produces: `api.listUsers/createUser/getUser/updateUser/deleteUser/resetUserPassword/getMe/updateMyPreferences`; `queryKeys.users.all`, `queryKeys.users.detail(id)`, `queryKeys.me`.

- [ ] **Step 1:** API client + types mirroring Task 4 schemas exactly.
- [ ] **Step 2:** `/admin/users` list — clone creators list skeleton: rows (initials avatar, username, display_name, admin badge, active dot, permission count, last_login), create Modal (username/password/display_name/is_admin + permission checkboxes rendered from `me.modules`), delete ConfirmDialog; row click → detail; `page-item` + `staggerDelay(i)` with the `listEntered` first-load guard (copy from creators page).
- [ ] **Step 3:** `/admin/users/[id]` detail — SectionPanel sections: 账号 (display_name, active toggle, admin toggle), 权限矩阵 (checkbox per module, disabled while is_admin), 配额 (MB input ↔ bytes, used readout), 内容过滤 (nsfw_visible), 操作 (reset password → Modal shows returned plaintext ONCE; delete). Mutations invalidate `queryKeys.users`.
- [ ] **Step 4:** Nav 管理 group: `["/admin/users", t("nav.users")]` rendered only when `me.is_admin`.
- [ ] **Step 5:** i18n zh/en for every new string.
- [ ] **Step 6:** `tsc` + build + deploy + manual check (create user / toggle / reset) + commit `feat(admin-web): users management pages`.

### Task 6 (A4): permission enforcement across routers + nav gating (single revertable commit)

**Files:**
- Modify (backend, router-level swap only): `works.py tags.py search.py creators.py repositories.py` → `RequirePermission("library")`; `curation.py` → `"curation"`; `subscriptions.py sources.py reference.py` → `"subscriptions"`; `download_jobs.py import_jobs.py tasks.py` → `"tasks"`; `admin.py system.py` → `"system"` (only where `RequireAdmin` already sits — unauthed health/ready endpoints untouched).
- Modify (frontend): create `admin-web/src/lib/usePermissions.ts`, create `admin-web/src/components/PermissionGuard.tsx`, modify `admin/layout.tsx` (filter nav by module).
- Test: `backend/tests/test_permission_matrix.py`

**Interfaces:**
- Produces: `usePermissions(): {isAdmin, has(module: string), isLoading}` wrapping the `queryKeys.me` query; `<PermissionGuard module="library">` renders children or a 403 `EmptyState` (i18n `common.forbidden`).

- [ ] **Step 1:** Backend swap, e.g. `works.py`: `router = APIRouter(dependencies=[RequireAdmin])` → `router = APIRouter(dependencies=[RequirePermission("library")])` (import from `app.auth`). Apply the full mapping above.
- [ ] **Step 2:** `test_permission_matrix.py`: one representative GET per module; module-user 200 on own module, 403 on each other module, admin 200 everywhere. Existing suite stays green (tests authenticate as bootstrap admin → is_admin=true after Task 3 backfill).
- [ ] **Step 3:** Frontend: nav link→module map; hide links the user lacks; wrap page contents in `PermissionGuard` (works/tags/search/creators→library; curation/dedup/merge-candidates→curation; subscriptions/sources/reference→subscriptions; jobs/import-jobs/scheduler/notifications→tasks; settings/data-mgmt/system→system; upload→upload later in Task 9).
- [ ] **Step 4:** Build + deploy both + manual matrix check with a limited user. Commit `feat(auth): enforce module permissions across API and navigation` — reverting THIS commit alone restores all-users-equal behavior.

### Task 7 (A5): preferences server-sync + NSFW filtering

**Files:**
- Modify (frontend): `theme.tsx`, `i18n.tsx`, `appearance.tsx` (server-first read, debounced write-through), `providers.tsx` (hydrate on me-load)
- Modify (backend): `repositories/work.py` (`list_all(..., force_sfw: bool = False)` injects `Work.is_nsfw == False`; `get` honors it too), `api/works.py` (routes receive `user: User = RequirePermission("library")` and pass `force_sfw=not user.nsfw_visible`), `services/search.py` (`search(..., force_sfw=False)` adds meili filter `is_nsfw = false`), `api/search.py`
- Test: `backend/tests/test_nsfw_filter.py`

- [ ] **Step 1:** Backend: repository/service params + route wiring; works detail returns 404 when `work.is_nsfw and force_sfw`.
- [ ] **Step 2:** Test: seed 1 NSFW + 1 SFW work; nsfw_visible=false user sees only SFW in list, 404 on NSFW detail; admin sees both; meili search call asserted to include the filter (monkeypatch `_client`).
- [ ] **Step 3:** Frontend: on me-load apply `preferences.{theme,palette,lang,appearance}` over localStorage; setters write localStorage AND fire one 800ms-debounced `api.updateMyPreferences(merged)`. Login page stays localStorage-only.
- [ ] **Step 4:** Full suite + build + deploy + commit `feat(users): server-side preferences and NSFW visibility filtering`.

### Task 8 (B1): manual upload backend

**Files:**
- Create: `backend/app/api/upload.py`, `backend/app/services/manual_upload.py`
- Modify: `backend/app/providers/manual.py` (implement all four `parse_*`), `backend/app/api/__init__.py` (`include_router(upload_router, prefix="/upload")`, router carries `RequirePermission("upload")`)
- Test: `backend/tests/test_manual_upload.py`

**Interfaces:**
- Produces: `POST /api/v1/upload` multipart (`files[]`, `title?`, `tags?` comma-joined, `is_nsfw?`, `creator_id?`) → `{work_id, download_job_id, import_job_id, used_bytes, quota_bytes}`; `ManualUploadService.save_upload(db, user, files, meta) -> UploadResult`.

- [ ] **Step 1 (service):** per-file validation — extension ∈ `{jpg,jpeg,png,webp,gif,mp4,webm}` AND magic-byte sniff (png `\x89PNG`, jpeg `\xff\xd8`, gif `GIF8`, webp `RIFF..WEBP`, mp4 `ftyp` at offset 4, webm `\x1a\x45\xdf\xa3`); size ≤ 500MB; quota check (`used + total > quota` → 413). Creator resolution: explicit `creator_id` requires curation permission (else 403) and existing creator; default = get-or-create personal space `source_creators(source="manual", source_creator_id=f"user:{username}")`. Files written as `{uuid4.hex}{ext}` under `DOWNLOAD_ROOT/manual/user_{username}/{work_uuid}/` with `resolve().relative_to(settings.download_root)` containment (else 400). One metadata JSON per work: `{"category":"manual","id":work_uuid,"title","tags":[...],"is_nsfw":bool,"uploaded_by":username,"files":[{"name","original_name","size"}],"date":"<iso8601>"}`. VERIFY FIRST: `DownloadJob.subscription_id/subscription_source_id` nullability — if nullable, create `DownloadJob(source="manual", source_url=f"manual://{work_uuid}", status="downloaded")` directly; if NOT nullable, provision a manual subscription per creator via the disk-import identity helper (`app/services/disk_identity.py`) exactly as `disk_import.py` does. Then `ArtifactLedger.upsert_many` rows (copy `disk_import.py` `artifact_row` usage) and `import_job_id = await _enqueue_import(str(job.id), new_json_paths={str(json_path)})` (import from `app.services.disk_import`). Finally `user.upload_used_bytes += total`.
- [ ] **Step 2 (provider):** implement `ManualProvider.parse_source_creator/parse_work_source/parse_assets/parse_source_tags` against the synthesized metadata; `parse_work_source` sets `is_enabled=True` explicitly (manual uploads are user-intentional — documented exception to the non-Pixiv default-disabled rule); return dict shapes copied from `app/providers/pixiv.py` equivalents.
- [ ] **Step 3 (api):** thin multipart route (`list[UploadFile]`), stream to a temp file then hand paths to the service; `ValueError`→400, quota→413; response model.
- [ ] **Step 4 (tests):** tmp `DOWNLOAD_ROOT` fixture + monkeypatched `_enqueue_import` (capture args): happy path (file on disk, contained path, ledger rows, job created, used_bytes grew); bad magic bytes→400; quota exceeded→413; `creator_id` without curation→403; personal space created once across two uploads. Plus one integration test running the real import runner over the synthesized JSON (follow `tests/test_disk_import.py` fixtures) asserting Work+Asset+WorkSource(source="manual") exist.
- [ ] **Step 5:** Full suite + deploy backend+workers + commit `feat(upload): manual upload endpoint, provider parsing, quota enforcement`.

### Task 9 (B2): upload page + uploader attribution

**Files:**
- Create: `admin-web/src/app/admin/upload/page.tsx`
- Modify: `admin-web/src/lib/api/index.ts` (`api.uploadWorks(form: FormData, onProgress?: (pct:number)=>void)` via XHR), `types.ts`, `admin/layout.tsx` (媒体库 group entry gated on `upload`), `works/[id]/page.tsx` (uploader line in source record when `source === "manual"` and `raw_metadata.uploaded_by`), `i18n.tsx` (`upload.*` zh+en)

- [ ] **Step 1:** Page: drag-drop + picker (accept mirrors backend list), batch fields (title/tags/nsfw), creator selector (locked to 个人空间 without curation permission; searchable creators dropdown with it), per-file rows with size/status, sequential submit with XHR `upload.onprogress` progress bars, success toast linking to created work; quota banner (`me.upload_used_bytes` / `upload_quota_bytes`), 413 surfaced inline.
- [ ] **Step 2:** Wrap in `PermissionGuard module="upload"`; add nav entry.
- [ ] **Step 3:** works detail uploader line; i18n zh/en.
- [ ] **Step 4:** `tsc` + build + deploy; manual E2E: upload 2 images → import completes → work visible with thumbnail, quota grew. Commit `feat(admin-web): manual upload page and uploader attribution`.

---

## Self-review notes

- Spec coverage: A1→T3, A2→T3, A3→T4, A4→T4 (last_login folded into T3 migration), A5→T7, A6→T5+T6+T7, B1→T8, B2→T9, §0→T1+T2. No gaps.
- Fact corrections vs spec: `users.is_active` already exists; routers use file-level `dependencies=[RequireAdmin]` so T6 is a one-line swap per file.
- Open verification points with stated fallbacks: T1 Step 1 (helper ownership), T8 Step 1 (DownloadJob FK nullability → disk_identity provisioning fallback).
- Type consistency: `RequirePermission` returns the `User` row (used by T7 works routes and T8 upload); `MeOut` fields match T5 `Me` type and T9 quota banner.
