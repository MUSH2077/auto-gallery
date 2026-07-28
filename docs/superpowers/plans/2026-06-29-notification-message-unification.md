# Notification & Message Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the bottom-right Toast the single feedback channel, back `/admin/notifications` with the durable `task_runs` feed, and record account login/password-change as instant `task_runs` events.

**Architecture:** Three isolated units — (1) backend `TaskService.record_account_event` + wiring into `auth_api.py`; (2) frontend feedback messages routed to `useToast`; (3) frontend `/admin/notifications` rewritten to read `api.listTasks`. No schema change: `task_runs` already carries `kind/operation_type/subject_type/subject_id/title/status/meta`.

**Tech Stack:** Backend — Python 3.12, FastAPI, SQLAlchemy 2.0 async, pytest. Frontend — Next.js 14, React 18, TypeScript, TanStack Query, Tailwind.

## Global Constraints

- Comments in code: English only. (Project rule.)
- Recording a `task_run` MUST never break the primary action (best-effort: `try/except`, log, continue). Copied from spec.
- Account events use `kind="account"`, `subject_id=None` (store `user_id` in `meta`) so repeated logins create **distinct** rows (the `create_task` deterministic-upsert only triggers when BOTH `subject_type` and `subject_id` are set).
- `operation_type` values: exactly `account-login` and `account-password-change`.
- Do NOT touch native `confirm()` dialogs. Do NOT add content-mutation audit. Do NOT add new tables.
- Backend tests run: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest <args>`.
- Frontend build check: `cd <repo-root>/admin-web && npm run build`.
- End every commit message with: `Co-Authored-By: Claude <noreply@anthropic.com>`.

---

### Task 1: Backend — `TaskService.record_account_event` helper

**Files:**
- Modify: `backend/app/services/tasks.py` (add a method to the `TaskService` class; insert after the existing `create_task` method, which ends around line 140 with `return task`).
- Test: `backend/tests/test_account_events.py` (create)

**Interfaces:**
- Consumes: existing `TaskService.create_task(*, kind, operation_type, title, status="enqueued", subject_type=None, subject_id=None, meta=None, ...) -> TaskRun` (flushes, does not commit; sets `finished_at` automatically when `status` is terminal e.g. `"complete"`). Existing `TaskService.list_tasks(kind=None, status=None, operation_type=None, source=None, q=None, offset=0, limit=50) -> tuple[int, list[TaskRun]]`.
- Produces: `async def record_account_event(self, *, action: str, username: str, user_id: int | None = None, ip: str | None = None) -> TaskRun`. `action` ∈ {`"login"`, `"password-change"`}. Creates a terminal (`status="complete"`) `task_run` with `kind="account"`, `operation_type=f"account-{action}"`, `subject_type="user"`, `subject_id=None`, `meta={"username": username, "user_id": user_id, "ip": ip}`. Flushes; the **caller commits**.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_account_events.py`:

```python
import pytest
from sqlalchemy import text


async def _clear_task_test_tables(db):
    await db.execute(text("""
        TRUNCATE task_events, task_runs RESTART IDENTITY CASCADE
    """))
    await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_account_event_creates_terminal_listable_event():
    from app.database import async_session, engine
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            svc = TaskService(db)

            task = await svc.record_account_event(
                action="login", username="admin", user_id=1, ip="198.51.100.10"
            )
            await db.commit()

            assert task.kind == "account"
            assert task.operation_type == "account-login"
            assert task.status == "complete"
            assert task.subject_id is None
            assert task.meta == {"username": "admin", "user_id": 1, "ip": "198.51.100.10"}
            assert task.finished_at is not None

            total, tasks = await svc.list_tasks(kind="account")
            assert total == 1
            assert tasks[0].operation_type == "account-login"
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_account_event_password_change_distinct_rows():
    from app.database import async_session, engine
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            svc = TaskService(db)

            await svc.record_account_event(action="login", username="admin", user_id=1)
            await svc.record_account_event(action="login", username="admin", user_id=1)
            await svc.record_account_event(action="password-change", username="admin", user_id=1)
            await db.commit()

            total, _ = await svc.list_tasks(kind="account")
            assert total == 3  # repeated logins are distinct rows, not upserted

            total_pw, pw = await svc.list_tasks(kind="account", operation_type="account-password-change")
            assert total_pw == 1
            assert pw[0].title == "Password changed"
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest tests/test_account_events.py -v`
Expected: FAIL — `AttributeError: 'TaskService' object has no attribute 'record_account_event'`.

- [ ] **Step 3: Implement `record_account_event`**

In `backend/app/services/tasks.py`, add this method to the `TaskService` class immediately after `create_task` (after its `return task` line):

```python
    # Account / user-management actions are recorded as instant terminal tasks so
    # they share the unified task_runs feed (/admin/notifications). subject_id is
    # left None on purpose — setting (subject_type, subject_id) would deterministically
    # upsert and collapse repeated logins into a single row, defeating the audit trail.
    _ACCOUNT_TITLES = {
        "login": "Signed in",
        "password-change": "Password changed",
    }

    async def record_account_event(
        self,
        *,
        action: str,
        username: str,
        user_id: int | None = None,
        ip: str | None = None,
    ) -> TaskRun:
        return await self.create_task(
            kind="account",
            operation_type=f"account-{action}",
            title=self._ACCOUNT_TITLES.get(action, action),
            status="complete",
            subject_type="user",
            subject_id=None,
            meta={"username": username, "user_id": user_id, "ip": ip},
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest tests/test_account_events.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/tasks.py backend/tests/test_account_events.py
git commit -m "feat(tasks): record_account_event helper for account audit

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Backend — record account events from login + change-password

**Files:**
- Modify: `backend/app/api/auth_api.py` (the `login` handler at lines 82-102 and the `change_password` handler at lines 122-141)
- Test: `backend/tests/test_account_events.py` (append)

**Interfaces:**
- Consumes: `TaskService(session).record_account_event(action=..., username=..., user_id=..., ip=...)` from Task 1.
- Produces: nothing new; side effect — a `kind="account"` `task_run` per successful login / password change. Best-effort: a recorder failure rolls back only the recording and never blocks the response.

- [ ] **Step 1: Write the failing test (append to `backend/tests/test_account_events.py`)**

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_records_account_event_and_survives_recorder_failure(monkeypatch):
    from app.api import auth_api
    from app.api.auth_api import LoginRequest, login
    from app.auth import hash_password
    from app.database import async_session, engine
    from app.models.user import User
    from app.services.tasks import TaskService
    from starlette.requests import Request

    def _req(ip: str = "198.51.100.10") -> Request:
        return Request({
            "type": "http", "http_version": "1.1", "method": "POST",
            "scheme": "http", "path": "/api/v1/auth/login", "query_string": b"",
            "headers": [], "client": (ip, 5000), "server": ("testserver", 80),
        })

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            await db.execute(text("DELETE FROM users WHERE username = 'acct_test'"))
            db.add(User(username="acct_test", password_hash=hash_password("secret123"),
                        is_active=True, must_change_password=False))
            await db.commit()

            # Bypass the Redis rate limiter for the test
            async def _ok(_ip):
                return True, 5, 0
            monkeypatch.setattr(auth_api._login_limiter, "check", _ok)

            resp = await login(LoginRequest(username="acct_test", password="secret123"), _req(), db)
            assert resp.access_token

            svc = TaskService(db)
            total, tasks = await svc.list_tasks(kind="account", operation_type="account-login")
            assert total == 1
            assert tasks[0].meta.get("username") == "acct_test"
            assert tasks[0].meta.get("ip") == "198.51.100.10"

            # Recorder failure must not break login
            async def _boom(**_kw):
                raise RuntimeError("recorder down")
            monkeypatch.setattr(TaskService, "record_account_event", _boom)
            resp2 = await login(LoginRequest(username="acct_test", password="secret123"), _req(), db)
            assert resp2.access_token  # login still succeeds
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM users WHERE username = 'acct_test'"))
            await _clear_task_test_tables(db)
            await db.commit()
        await engine.dispose()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest tests/test_account_events.py::test_login_records_account_event_and_survives_recorder_failure -v`
Expected: FAIL — `assert total == 1` fails (no event recorded yet; `total == 0`).

- [ ] **Step 3: Implement the login recorder**

In `backend/app/api/auth_api.py`, add near the top with the other imports (after line 21):

```python
import logging

from app.services.tasks import TaskService

logger = logging.getLogger(__name__)
```

Then in `login`, replace the final two lines of the handler:

```python
    must_change_password = bool(user.must_change_password)
    token = create_access_token(user.username, must_change_password=must_change_password)
    return TokenResponse(access_token=token, must_change_password=must_change_password)
```

with:

```python
    must_change_password = bool(user.must_change_password)
    token = create_access_token(user.username, must_change_password=must_change_password)

    # Best-effort account audit — never block login if recording fails.
    try:
        await TaskService(session).record_account_event(
            action="login", username=user.username, user_id=user.id, ip=client_ip,
        )
        await session.commit()
    except Exception:  # noqa: BLE001 — audit is best-effort
        await session.rollback()
        logger.warning("Failed to record account-login event", exc_info=True)

    return TokenResponse(access_token=token, must_change_password=must_change_password)
```

- [ ] **Step 4: Implement the change-password recorder**

In `change_password`, replace:

```python
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    user.updated_at = datetime.now(timezone.utc)
    await session.commit()
    token = create_access_token(user.username, must_change_password=False)
    return {"ok": True, "access_token": token, "token_type": "bearer", "must_change_password": False}
```

with:

```python
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    user.updated_at = datetime.now(timezone.utc)
    await session.commit()
    token = create_access_token(user.username, must_change_password=False)

    # Best-effort account audit, in its own transaction after the password commit.
    try:
        await TaskService(session).record_account_event(
            action="password-change", username=user.username, user_id=user.id,
        )
        await session.commit()
    except Exception:  # noqa: BLE001 — audit is best-effort
        await session.rollback()
        logger.warning("Failed to record account-password-change event", exc_info=True)

    return {"ok": True, "access_token": token, "token_type": "bearer", "must_change_password": False}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest tests/test_account_events.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Run the auth + tasks suites for regressions**

Run: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest tests/test_auth_password_rotation.py tests/test_tasks.py -v`
Expected: PASS (no regressions).

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/auth_api.py backend/tests/test_account_events.py
git commit -m "feat(auth): record login + password-change as account task events

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Frontend — route login + profile feedback through Toast

**Files:**
- Modify: `admin-web/src/app/admin/login/page.tsx`
- Modify: `admin-web/src/app/admin/settings/profile/page.tsx`

**Interfaces:**
- Consumes: `useToast()` from `@/components/Toast` → `{ success, error, info, warning }`, each accepting a string or `{ title?, message }`.
- Produces: no exported symbols; UI behavior change only.

- [ ] **Step 1: Update `login/page.tsx`**

Add the import after line 5 (`import { useT } from "@/lib/i18n";`):

```tsx
import { useToast } from "@/components/Toast";
```

Replace the hook block (lines 9-15) — delete `const [error, setError] = useState("");` and add the toast hook:

```tsx
  const t = useT();
  const { login, isAuthenticated, user } = useAuth();
  const router = useRouter();
  const toast = useToast();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
```

In `handleSubmit`, replace its body (lines 23-33) so the catch uses the toast (the leading `setError("")` is removed):

```tsx
    setLoading(true);
    try {
      const authUser = await login(username, password);
      router.replace(authUser.must_change_password ? "/admin/settings/profile" : "/admin");
    } catch (err: unknown) {
      toast.error(err instanceof Error ? err.message : t("auth.invalid_credentials"));
    } finally {
      setLoading(false);
    }
```

Delete the inline error block (lines 82-86):

```tsx
            {error && (
              <div className="rounded-md border border-danger/30 bg-danger-subtle px-3 py-2 text-sm text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger">
                {error}
              </div>
            )}
```

- [ ] **Step 2: Update `settings/profile/page.tsx`**

Add the import after line 4 (`import { useT } from "@/lib/i18n";`):

```tsx
import { useToast } from "@/components/Toast";
```

Replace the hook block (lines 9-16) — remove `error`/`success` state, add toast:

```tsx
  const t = useT();
  const { user, updateAccessToken } = useAuth();
  const toast = useToast();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [loading, setLoading] = useState(false);
```

Replace `handleSubmit` (lines 18-45) so validation + result use toasts:

```tsx
  async function handleSubmit(e: FormEvent) {
    e.preventDefault();

    if (newPassword.length < 6) {
      toast.error(t("auth.password_too_short"));
      return;
    }
    if (newPassword !== confirmPassword) {
      toast.error(t("auth.password_mismatch"));
      return;
    }

    setLoading(true);
    try {
      const refreshed = await authChangePassword(currentPassword, newPassword);
      await updateAccessToken(refreshed.access_token);
      toast.success(t("auth.change_password_success"));
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (err: unknown) {
      toast.error(err instanceof Error ? err.message : t("auth.change_password_failed"));
    } finally {
      setLoading(false);
    }
  }
```

Delete the inline error + success blocks (lines 115-124):

```tsx
          {error && (
            <div className="text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg px-3 py-2">
              {error}
            </div>
          )}
          {success && (
            <div className="text-sm text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-lg px-3 py-2">
              {t("auth.change_password_success")}
            </div>
          )}
```

(The `must_change_password` amber banner at lines 67-72 stays — it is a status banner, not action feedback.)

- [ ] **Step 3: Build to verify no type/compile errors**

Run: `cd <repo-root>/admin-web && npm run build`
Expected: build completes; no TypeScript error about unused `error`/`success`/`setError`/`setSuccess` (removed) and no missing-import errors.

- [ ] **Step 4: Commit**

```bash
git add admin-web/src/app/admin/login/page.tsx admin-web/src/app/admin/settings/profile/page.tsx
git commit -m "feat(admin-web): route login + profile feedback through Toast

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Frontend — rewrite `/admin/notifications` as the durable task feed

**Files:**
- Modify (full rewrite): `admin-web/src/app/admin/notifications/page.tsx`
- Modify: `admin-web/src/lib/i18n.tsx` (add the new keys to the `zh` map near the existing `notifications.*` entries)

**Interfaces:**
- Consumes: `api.listTasks({ kind?, offset?, limit? }) -> Promise<TaskRunListResponse>` where `TaskRunListResponse = { total: number; items: TaskRun[] }` and `TaskRun` has `id, kind, operation_type, status, title, source, progress_current, progress_total, created_at, finished_at`. Also `queryKeys.tasks.all = ["tasks"]`. Components `PageHeader`, `EmptyState`, `ErrorState`, `StatusBadge`, `SourceBadge` from `@/components`.
- Produces: no exported symbols (default page component only).

- [ ] **Step 1: Add i18n keys**

In `admin-web/src/lib/i18n.tsx`, in the `zh` map (begins `const zh: Record<string, string> = {` at line 10), add these entries next to the other `notifications.*` keys:

```tsx
  "notifications.filter_all": "全部",
  "notifications.filter_tasks": "任务",
  "notifications.filter_account": "账户",
  "notifications.load_more": "加载更多",
  "notifications.account_login": "登录",
  "notifications.account_password_change": "修改密码",
```

- [ ] **Step 2: Rewrite the notifications page**

Replace the entire contents of `admin-web/src/app/admin/notifications/page.tsx` with:

```tsx
"use client";
import { useMemo, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useT } from "@/lib/i18n";
import { api, queryKeys } from "@/lib/api";
import type { TaskRun } from "@/lib/api/types";
import { PageHeader, EmptyState, ErrorState, StatusBadge, SourceBadge } from "@/components";

type Filter = "all" | "tasks" | "account";
const PAGE_SIZE = 50;

// "tasks" = the long-running pipeline kinds; "account" = audit events.
const TASK_KINDS = ["download", "import", "admin"];

function taskLink(task: TaskRun): string | null {
  if (task.kind === "download" || task.kind === "import") {
    return `/admin/jobs?tab=${task.kind}&task=${task.id}`;
  }
  return null;
}

function timeStr(iso?: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString();
}

export default function NotificationsPage() {
  const t = useT();
  const router = useRouter();
  const [filter, setFilter] = useState<Filter>("all");

  // Account is a single-kind server filter; "tasks" has no single kind param,
  // so it fetches all and filters client-side. "all" fetches everything.
  const kindParam = filter === "account" ? "account" : undefined;

  const query = useInfiniteQuery({
    queryKey: [...queryKeys.tasks.all, "feed", filter],
    queryFn: ({ pageParam = 0 }) =>
      api.listTasks({ kind: kindParam, offset: pageParam as number, limit: PAGE_SIZE }),
    initialPageParam: 0,
    getNextPageParam: (lastPage, pages) => {
      const loaded = pages.reduce((n, p) => n + p.items.length, 0);
      return loaded < lastPage.total ? loaded : undefined;
    },
  });

  const items = useMemo(() => {
    const all = query.data?.pages.flatMap((p) => p.items) ?? [];
    if (filter === "tasks") return all.filter((task) => TASK_KINDS.includes(task.kind));
    return all;
  }, [query.data, filter]);

  const filters: { key: Filter; label: string }[] = [
    { key: "all", label: t("notifications.filter_all", "全部") },
    { key: "tasks", label: t("notifications.filter_tasks", "任务") },
    { key: "account", label: t("notifications.filter_account", "账户") },
  ];

  return (
    <main className="max-w-3xl mx-auto p-6 md:p-10 page-transition">
      <PageHeader title={t("notifications.title")} description={t("notifications.desc")} />

      <div className="mb-4 flex gap-1">
        {filters.map((f) => (
          <button
            key={f.key}
            onClick={() => setFilter(f.key)}
            className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
              filter === f.key
                ? "bg-accent-subtle text-accent"
                : "text-muted hover:bg-subtle hover:text-fg"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {query.isError ? (
        <ErrorState onRetry={() => query.refetch()} />
      ) : query.isLoading ? (
        <div className="space-y-3">
          {[0, 1, 2].map((i) => (
            <div key={i} className="card h-20 animate-pulse" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <EmptyState title={t("notification.empty")} />
      ) : (
        <div className="space-y-3">
          {items.map((task) => {
            const link = taskLink(task);
            const pct =
              task.progress_total && task.progress_current !== undefined && task.progress_total > 0
                ? Math.round(((task.progress_current ?? 0) / task.progress_total) * 100)
                : null;
            return (
              <div
                key={task.id}
                className={`card p-4 ${link ? "cursor-pointer hover:border-accent/50" : ""}`}
                onClick={() => link && router.push(link)}
              >
                <div className="flex items-start gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="mb-1 flex items-center gap-2">
                      <StatusBadge status={task.status} />
                      {task.source && <SourceBadge source={task.source} />}
                      <h3 className="truncate text-sm font-semibold">
                        {task.title || task.operation_type || task.kind}
                      </h3>
                    </div>
                    {pct !== null && task.status !== "complete" && (
                      <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-subtle dark:bg-border">
                        <div
                          className="h-full rounded-full bg-accent transition-all duration-500"
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                    )}
                    <p className="mt-1 text-[10px] text-muted tabular">{timeStr(task.created_at)}</p>
                  </div>
                </div>
              </div>
            );
          })}

          {query.hasNextPage && (
            <div className="flex justify-center pt-2">
              <button
                onClick={() => query.fetchNextPage()}
                disabled={query.isFetchingNextPage}
                className="btn-ghost px-4 py-1.5 text-xs"
              >
                {t("notifications.load_more", "加载更多")}
              </button>
            </div>
          )}
        </div>
      )}
    </main>
  );
}
```

- [ ] **Step 3: Verify `ErrorState` accepts an `onRetry` prop**

Run: `grep -nE "onRetry|interface .*Props|function ErrorState" <repo-root>/admin-web/src/components/ErrorState.tsx`
Expected: shows an `onRetry` prop. If `ErrorState` does NOT take `onRetry`, change the error branch to `<ErrorState />` (drop the prop) and add a separate retry button, or use the prop name it actually exposes. Do not invent a prop.

- [ ] **Step 4: Build to verify**

Run: `cd <repo-root>/admin-web && npm run build`
Expected: build completes with no TypeScript errors. (The old page imported `useNotifications` / `BatchJobState`; the rewrite drops them — confirm no other file imports symbols removed from this page. It does not export any.)

- [ ] **Step 5: Commit**

```bash
git add admin-web/src/app/admin/notifications/page.tsx admin-web/src/lib/i18n.tsx
git commit -m "feat(admin-web): /admin/notifications reads durable task_runs feed

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Deploy + smoke check

**Files:** none (deploy only)

- [ ] **Step 1: Run the full backend suite**

Run: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest -q`
Expected: all pass.

- [ ] **Step 2: Rebuild + restart the affected services**

Per the project Completion Workflow (backend code is baked into the image):

```bash
cd <repo-root>
docker compose build --build-arg CACHEBUST="$(date +%s)" --pull backend admin-web
docker compose up -d --force-recreate backend worker-download worker-import worker-operations scheduler admin-web
```

- [ ] **Step 3: Manual smoke check**

- Log out and back in; confirm a bottom-right Toast on a wrong password.
- After login, open `/admin/notifications` → the "账户" filter shows a "Signed in" entry; reload the page → it persists (server-backed).
- Trigger a download/import or an admin operation; confirm it appears under "任务" / "全部".
- Change the password on `/admin/settings/profile`; confirm a success Toast (no inline banner) and a "Password changed" entry in notifications.

---

## Self-Review

**Spec coverage:**
- Part 1 (feedback → Toast): Task 3. ✓
- Part 2 (`/admin/notifications` = server `task_runs` feed, filters, states, pagination, bell unchanged): Task 4. ✓ (bell file untouched.)
- Part 3 (account login + change-password → instant `task_runs`, `kind="account"`, best-effort): Tasks 1 + 2. ✓
- Error handling (best-effort recorder; `ErrorState` + retry): Task 2 Steps 3/4, Task 4 Steps 2/3. ✓
- Testing (backend pytest for event creation + recorder-failure isolation; frontend build): Tasks 1, 2, 3, 4. ✓
- `subject_id` left null / `user_id` in `meta`: Task 1 (Global Constraints + helper). ✓
- Out of scope respected (no `confirm()`, no new table, no content audit). ✓

**Type consistency:** `record_account_event(action, username, user_id, ip)` defined in Task 1, called identically in Task 2. `api.listTasks({ kind, offset, limit })` and `TaskRunListResponse`/`TaskRun` fields match `types.ts`. `kind="account"` / `operation_type="account-login"|"account-password-change"` consistent across backend + frontend filter. Component names (`PageHeader/EmptyState/ErrorState/StatusBadge/SourceBadge`) match `components/index.ts`.

**Placeholder scan:** none — every code step has complete code; Task 4 Step 3 explicitly guards the one external-prop assumption (`ErrorState.onRetry`) with a verification command instead of assuming.
