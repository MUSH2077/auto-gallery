# Frontend Optimization — Phase 1 Design (Style Unification + Responsiveness)

**Date:** 2026-06-29
**Status:** Approved (brainstorming) — pending implementation plan

## Goal

Unify the admin-web visual style so semantic colors follow the active palette
(true cross-theme consistency), and make the UI feel faster — without changing
the GitHub default theme's appearance.

## Core principles / constraints

- **Zero visual regression in the GitHub default theme.** The migration maps
  hardcoded colors to the semantic tokens whose GitHub values are the same hue
  (`accent`=#0969da blue, `danger`=#cf222e red, `success`=#1a7f37 green,
  `warning`=#9a6700 yellow), preserving each element's fill/outline shape.
- **Migrate by color-utility → token, preserving shape.** Do NOT force existing
  filled buttons into `.btn-primary`/`.btn-danger` (the existing `.btn-primary`
  is a *green fill* and `.btn-danger` is a *red outline* — reshaping them would
  change appearance). Buttons keep their fill; only the color class changes.
- **Exclude per-source / brand colors.** `src/lib/sourceColors.ts` (84 of the
  matched occurrences) and any per-source badge/brand color stay hardcoded — this
  is required by the `source-colors` constraint.
- **Override the "don't mass-rename" note for this effort.** `DESIGN-TOKENS.md`
  says migrate opportunistically; for this phase we do a deliberate, *batched*
  migration, verifying no visual change per batch (honoring the spirit).

## Background (measured)

- 413 hardcoded `(bg|text|border)-(blue|red|green|yellow|amber|slate|gray|zinc|neutral)-NNN`
  usages; 84 are in `sourceColors.ts` (excluded) → **~329 in scope**.
- QueryClient defaults are healthy (`staleTime 30s`, `refetchOnWindowFocus:false`,
  `retry 1`). 13 files poll; the worst are `scheduler` (2000ms) and `logs` (5000ms).
  `jobs/page.tsx` already has the good adaptive pattern (`REFETCH_ACTIVE_MS=8000`,
  `REFETCH_IDLE_MS=30000`).
- 7 raw `<img>` thumbnail tags across 6 files; 0 `next/image`; lazy-loading is uneven.
- The bell (`NotificationCenter`, in-memory) and `/admin/notifications` (server
  `task_runs`) now have different data sources — a consistency gap to close.

## Architecture — three tracks, isolated units

### Track A — Semantic color tokens + `Banner` primitive (the bulk)

**Mapping table** (collapse any `X dark:Y` pair into the single token — tokens
auto-flip in dark mode, so the `dark:` variant is dropped):

| Hardcoded (incl. `dark:` pair) | → Token class |
|---|---|
| `text-blue-300..700` (links/icons) | `text-accent` |
| `bg-blue-600/700` + `text-white` (filled button) | `bg-accent text-white` (+ `hover:bg-accent/90`) |
| `bg-blue-50` / `bg-blue-900/..` (tint) | `bg-accent-subtle` |
| `bg-blue-100 text-blue-700` (avatar block) | `bg-accent-subtle text-accent` |
| `border-blue-200/..` | `border-accent/30` |
| `text-red-400..700` | `text-danger` |
| `bg-red-600` + `text-white` (filled) | `bg-danger text-white` |
| `bg-red-50` / `bg-red-900/..` | `bg-danger-subtle` |
| `border-red-200/800` | `border-danger/30` |
| `text-green-400..700` | `text-success` |
| `bg-green-50` / `bg-green-900/..` | `bg-success-subtle` |
| `text-yellow-600` / `text-amber-*` | `text-warning` |
| yellow/amber banner bg + border | `bg-warning-subtle` + `border-warning/30` |
| residual `slate/gray/zinc/neutral` text | `text-muted` / `text-fg` (by role) |
| residual gray bg / border | `bg-subtle` / `border-border` |

**`Banner` primitive** — `src/components/Banner.tsx`:
- Props: `tone: "info" | "success" | "warning" | "danger"`, `title?: string`, `children`.
- Renders the recurring bordered tinted box (`border border-<tone>/30 bg-<tone>-subtle text-<tone>`).
- Replaces inline tinted-banner `<div>`s (profile must-change, data-mgmt warnings, backup notices, etc.). Exported from `components/index.ts`.

**Batches** (each batch: edit → `npm run build` → visual spot-check → commit):
1. `reference/danbooru/page.tsx` (96)
2. `data-mgmt/page.tsx` (76) + `settings/data-mgmt/page.tsx` (22)
3. `jobs/page.tsx` (34) + `components/NotificationCenter.tsx` (13)
4. settings group: `settings/backup` (25), `settings/proxy` (15), `settings/profile` (13), `settings/auth-status` (13), `settings/logs` (10), `settings/gallerydl` (9), `settings/download-defaults` (9)
5. `search/page.tsx` (11), `creators/[id]/mapping/page.tsx` (11), `tags/[id]/page.tsx` (8)

Files with <8 occurrences are out of this phase (opportunistic later). The
`Banner` extraction happens within the batch that first needs it (batch 2 or 4),
then reused in later batches.

### Track B — Responsiveness (two cheap wins)

**B1 — Polling discipline.** New `src/lib/polling.ts` exporting the shared
constants `POLL_ACTIVE_MS = 8000` and `POLL_IDLE_MS = 30000` (lifting the values
already used in `jobs/page.tsx`) plus a tiny helper
`pollInterval(active: boolean) => number`. Apply it to the high-frequency pollers:
- `scheduler/page.tsx`: `2000` → adaptive (active while a sync/op is running, else idle).
- `settings/logs/page.tsx`: keep "only when `autoRefresh`", value from the shared constant.
- Other pollers that hardcode `15000`/`12000` adopt the shared constants where they map cleanly. `jobs/page.tsx` switches its local consts to import from `lib/polling.ts` (no behavior change). `refetchIntervalInBackground` stays default (false).

**B2 — Images.** Add `loading="lazy" decoding="async"` to every thumbnail `<img>`
(6 files: `tags/[id]`, `creators/[id]`, `search`, `repositories/[id]`, the admin
dashboard `page.tsx`, `curation`) and the works-grid thumbnail element (locate its
component during implementation). Backend: in `backend/app/api/media.py` `thumb()`,
set `Cache-Control: public, max-age=86400` on the returned `FileResponse` (scoped
to `/media/thumb` only — `preview`/`original` are auth-gated and stay uncached).

### Track C — Bell / notifications data-source consistency

`NotificationBell` (in `NotificationCenter.tsx`) currently lists in-memory
`items`. Change its dropdown list to be sourced from the server `task_runs` feed
(`api.listTasks({ limit: ~10 })`) for the "recent" section, while keeping the
in-session `batchJob` / `operationJob` realtime overlays at the top. Sections are
relabeled to read "进行中 / 最近" (in-progress / recent). This makes the bell and
`/admin/notifications` share one data source. The page itself is unchanged.

## Data flow

No new data flow. Track A/B are presentational/perf; Track C points the bell list
at the existing `GET /api/v1/tasks` endpoint already used by the page.

## Error handling

- Track C: if `listTasks` fails in the bell, show nothing extra (the realtime
  overlays still render); do not block the dropdown.
- Backend `Cache-Control` is additive and cannot fail the response.

## Testing

- No frontend unit-test runner is assumed → each Track A batch and Track B/C
  change is verified by `cd admin-web && npm run build` + a visual spot-check
  (and, for Track A, confirming no `dark:` color residue and no palette breakage
  by toggling a non-GitHub palette).
- Backend: a pytest asserting `/media/thumb/{id}` responds with a `Cache-Control`
  header (and that `/media/preview` / `/media/original` do NOT gain caching).

## Out of scope (YAGNI / later phases)

- RSC migration (57 `"use client"` files), `next/image` migration.
- Native `confirm()` → styled `ConfirmDialog`.
- Long-tail files with <8 hardcoded-color occurrences.
- `sourceColors.ts` and any per-source/brand colors (kept hardcoded by constraint).
- Forcing existing buttons into `.btn-*` classes (shape change — avoided).

## Definition of done

- The in-scope files use semantic tokens; switching to a non-GitHub palette
  reskins their badges/banners/buttons/text correctly.
- GitHub default theme looks unchanged (visual spot-check per batch).
- `scheduler`/`logs` no longer poll at 2s/5s; thumbnails lazy-load and `/media/thumb`
  is cacheable.
- The bell and `/admin/notifications` show the same (server) recent tasks.
- `admin-web` build is green; the backend Cache-Control test passes.
