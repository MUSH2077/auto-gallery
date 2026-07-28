# Dashboard design QA

## Scope and evidence

- Selected desktop reference: generated concept 3, “Content and task pulse”
  (session asset, intentionally not committed).
- Selected mobile reference: the approved responsive continuation of concept 3
  (session asset, intentionally not committed).
- Desktop implementation:
  `docs/assets/admin-dashboard.png` at a 1440×1024 viewport, full dashboard
  capture (1440×1105 output), dark theme, English, reduced motion.
- Mobile implementation: temporary Playwright evidence at 390×844, full
  dashboard capture (390×1921 output), dark theme, English, reduced motion.
- Combined desktop and mobile comparisons were generated as temporary,
  untracked QA evidence.

The reference and implementation were reviewed together in each combined
comparison image. The README screenshot flow fixes the clock at
2026-07-28T08:30:00Z and intercepts every API and media request with fictional
fixtures.

## Visual review

| Area | Result | Notes |
|---|---|---|
| Shell and hierarchy | Passed | Existing sidebar, top bar, tokens, borders, typography, and dark theme remain consistent with the product and reference. |
| Operational status | Passed | Six integrated cells preserve the reference hierarchy and become a 2×3 grid on mobile. Status dots, values, destinations, refresh state, and update time remain legible. |
| Recent works | Passed | Three generated fictional thumbnails use consistent crops and metadata. Mobile uses a horizontal snap flow without a heavy section wrapper. |
| Live activity | Passed | Download, import, and completed syncs are stably sorted and grouped. Mobile rows remain compact, do not wrap into page overflow, and keep the task information as a 44px touch target. |
| Attention state | Passed | The alert is persistent while backend attention counts remain non-zero. Primary and secondary actions stack on mobile. Singular issue copy is correct. |
| Services | Passed | The four core services match the desktop reference and form an exact 2×2 mobile grid. |
| Responsive layout | Passed | Desktop uses two columns, tablet stacks sections, and mobile keeps horizontal work scrolling local to the work rail. No page-level horizontal overflow at 1440, 768, or 390px. |
| Text overflow | Passed | Long task, creator, source, and service values truncate only inside bounded metadata slots; the update time remains visible at 390px. |
| Theme and motion | Passed | Dark screenshots are stable, reduced-motion content appears immediately, and no essential state depends on animation. |

## Fix history

- P1: Removed the mobile overlap between the long `danbooru` badge and activity
  title by reserving a bounded source column.
- P2: Replaced wrapping mobile activity rows with a compact single-row
  structure and made the full task-information block navigable.
- P2: Removed the heavy recent-works wrapper on mobile and reduced the mobile
  heading size to avoid avoidable wrapping.
- P2: Limited the dashboard service summary to the four core services so the
  mobile layout is a complete 2×2 grid without an empty fifth cell.
- P2: Added singular issue text, prevented the update timestamp from clipping,
  and captured the complete desktop dashboard for README use.
- P2: Added missing English job-toolbar strings that previously rendered as
  Chinese fallback text in the English screenshot.

No open P0, P1, or P2 findings remain.

## Interaction and runtime evidence

- Status links: scheduler, jobs, failed jobs, lost-heartbeat jobs, storage.
- Work navigation: whole work card opens the corresponding work.
- Task navigation: task-information target and desktop chevron open the
  corresponding download/import view.
- Mutations: manual refresh, single download/import retry, and retry-all-failed
  use the existing APIs and report success/failure with toasts.
- Failure behavior: a failed manual refresh preserves the last successful
  dashboard payload.
- Permissions: retry controls are absent without the `tasks` permission.
- Keyboard and touch: focus checks pass at 768 and 390px; controls and
  navigable information targets meet the 44px mobile target.
- Browser console/page errors: none in the focused dashboard and sanitized
  screenshot runs (expected unavailable test WebSocket noise is excluded).
- Framework overlay: absent.

Validation:

- `npm run typecheck`
- `npm run build`
- dashboard Playwright suite: 6 passed
- complete Playwright suite: 9 passed, 1 documentation generator skipped by
  design (it passes through `npm run screenshots:readme`)
- README screenshot generator: 1 passed, all routes strictly intercepted

final result: passed
