# Notification & Message Unification — Design

**Date:** 2026-06-29
**Status:** Approved (brainstorming) — pending implementation plan

## Goal

Unify the admin-web messaging surface so that (1) every feedback message from a
user action surfaces through the single bottom-right Toast channel, and (2)
`/admin/notifications` becomes a durable, server-backed feed that reflects both
long-running tasks and account/user-management actions.

## Background — current state

Three message channels exist and are inconsistent:

1. **Bottom-right Toast** (`src/components/Toast.tsx`, `fixed bottom-4 right-4`) —
   the intended channel; 40 `useToast` call sites; `success | error | info | warning`.
2. **Inline banners** — `src/app/admin/login/page.tsx` and
   `src/app/admin/settings/profile/page.tsx` render results via local
   `setError` / `setMessage` state instead of the Toast (this is the "other channel").
3. **Native `confirm()`** — jobs / works / curation / search pages use the browser
   confirm dialog. These are *confirmations*, not feedback messages — **out of scope**.

The notification surface has two views, both ephemeral and inconsistent:

- The top-bar **bell** (`src/components/NotificationCenter.tsx`) shows in-memory
  `ActivityItem[]` + `batchJob` + `operationJob`; lost on reload.
- **`/admin/notifications`** (`src/app/admin/notifications/page.tsx`) reads the *same*
  in-memory context but renders only `batchJob` + `items` (it **omits `operationJob`**),
  also lost on reload.

A durable source already exists and is unused by the notifications page: the backend
`task_runs` table + `GET /api/v1/tasks` (`api.listTasks`), already consumed by
`/admin/jobs`. Long-running work (download / import / batch import / backup / reindex /
disk-import / clear) already persists there as `task_runs` of `kind` ∈ {download, import, admin}.

## Decisions (from brainstorming)

- **Audit breadth:** Account actions (login, password change) recorded as instant
  `task_runs` events, plus the existing long-running tasks. No content-mutation audit.
- **Notifications page source:** Server-side `task_runs` as the single source of truth;
  the bell remains the session-realtime overlay.
- **Confirm scope:** Native `confirm()` left untouched; only feedback messages are unified.

## Architecture

Three isolated units. **No schema change** — `task_runs` already carries
`kind / operation_type / subject_type / subject_id / title / status / meta`.

```
Part 1 (frontend, local)   login/profile inline banners → useToast
Part 2 (frontend, rewrite) /admin/notifications → api.listTasks (durable feed)
Part 3 (backend)           auth login/change-password → record instant task_run (kind="account")
```

### Part 1 — Feedback messages → bottom-right Toast

- **Change:** Replace the local `setError` / `setMessage` inline banners in
  `src/app/admin/login/page.tsx` and `src/app/admin/settings/profile/page.tsx` with
  `useToast` (`error` / `success`). `ToastProvider` already wraps the whole app at
  `src/app/providers.tsx` (root), so Toast is available on the pre-auth login page.
- **Leave unchanged:** field-level inline validation (immediate validation, not a
  message popup); page-level load-error states (`ErrorState` / `ErrorBoundary` are data
  states, not action feedback); native `confirm()`.

### Part 2 — `/admin/notifications` = server `task_runs` feed

- **Rewrite** `src/app/admin/notifications/page.tsx` to read
  `api.listTasks({ limit, offset, kind? })` via TanStack Query instead of the in-memory
  `useNotifications()` context. Survives reload; paginated.
- **Row content:** `StatusBadge` + `title` + source badge + progress + timestamp + link
  (download/import → `/admin/jobs?...`; account → no link).
- **Top filter:** All / Tasks (`download` + `import` + `admin`) / Account (`account`).
  Implemented by passing `kind` to `listTasks` where it maps to a single value
  (Account → `kind="account"`), and the "Tasks" view issues the request without `kind`
  and hides `account` rows client-side. The plan picks the simplest variant that keeps
  one query per view.
- **States:** empty state, loading state, pagination ("load more" or pager) and an
  `ErrorState` + retry on `listTasks` failure.
- **Bell unchanged:** remains the in-session realtime overlay; its existing
  "view all → /admin/notifications" link stays. Responsibilities split cleanly:
  bell = realtime/unread, page = durable/full history.

### Part 3 — Account actions → instant `task_runs` events (backend)

- After a successful `login` and `change-password` in
  `backend/app/api/auth_api.py`, write one **terminal** task_run:
  - `kind="account"`
  - `operation_type` ∈ { `account-login`, `account-password-change` }
  - `subject_type="user"`
  - `title` (e.g. "Signed in", "Password changed")
  - `status="complete"`
  - `meta={ "username": ..., "user_id": <user.id>, "ip": <login only> }`
- **Note:** there is currently no profile/display-name update endpoint, so
  `account-profile-update` is intentionally omitted (add later if profile editing ships).
- **Implementation:** add a thin `TaskService.record_account_event(...)` helper that
  calls the existing create path with `status="complete"` (and `enqueued_at` /
  `finished_at` set to now). Reuses `task_payload` for serialization.
- `kind="account"` is distinct from download/import/admin, so account events **do not
  pollute `/admin/jobs`** (its tabs query specific kinds).
- The login endpoint already receives `request: Request`, so client IP is available.

### Note on `task_runs.subject_id` type

`subject_id` is a `UUID | None` column, but `User.id` is an integer PK. To avoid a
migration, the account user id is stored in `meta` (`meta.user_id`) and `subject_id`
is left null. (Re-confirm against the model during planning.)

## Data flow

- **Account action:** frontend call → backend executes + records `task_run(complete)` →
  frontend shows Toast (realtime). On reload / later, `/admin/notifications` reads
  `listTasks` and shows the entry.
- **Long-running task:** unchanged — already persisted as `task_runs` → same feed.

## Error handling

- Recording a `task_run` must **never** break the primary action: wrap in
  `try/except`, log on failure, and let login/change-password succeed regardless
  (best-effort audit).
- `listTasks` failure on the notifications page → `ErrorState` + retry.

## Testing

- **Backend (pytest):** after a successful login and change-password, assert a
  `task_run` with the expected `kind="account"` / `operation_type` / `status="complete"`
  is created. Assert that a forced failure in the recorder does not block the primary
  operation (login still returns a token).
- **Frontend:** `admin-web` build passes; notifications page renders `listTasks`
  results (visual / manual check), including empty/loading/error states.

## Out of scope (YAGNI)

- Replacing native `confirm()` with a styled `ConfirmDialog`.
- Content-mutation audit (delete creator, toggle subscription, merge tag, …).
- New tables; read/unread state machine (the bell's in-session count is enough).
- Multi-user CRUD / user management UI.

## Known side effect

Data Management → "clear jobs/all" already clears `task_runs`, so it will also clear the
account-action audit. Acceptable for this iteration.
