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

# Primary admin navigation and layout QA

## Scope and evidence

- Baseline: the Jobs page shell, header geometry, and 24px header-to-content
  rhythm.
- Primary routes: Works, Tags, Upload, Danbooru, Creators, Subscriptions,
  Jobs, Scheduler, Data Management, System Status, and Settings.
- Viewports: 1440×960, 768×1024, and 390×844.
- Theme and locale combinations: English dark theme and Chinese light theme.
- All route and screenshot requests used fictional, intercepted fixtures.

The original navigation defect was reproduced before implementation: Upload
normally placed its title at 102px, but returning from another page left
`scrollY=56` and moved the title to 46px, under the sticky top bar. After the
fix, pathname transitions end at `scrollY=0`, focus `#main-content` without
scrolling it, and keep the title below the sticky bar. Query-only transitions
retain the current scroll position.

## Route matrix

| Area | Result | Notes |
|---|---|---|
| Shell and header geometry | Passed | All 11 routes match the Jobs page shell x-position and width; headings share the same baseline and first content begins 24px below the header. |
| Library | Passed | Works uses the shared toolbar and segmented controls; Tags uses the theme card and keeps the bubble layout intact. |
| Upload and import | Passed | Upload and Danbooru use the same shell, banner, section, and action primitives without page-level overflow. |
| Sources | Passed | Creators and Subscriptions no longer add duplicate post-filter spacing; segmented controls wrap safely on mobile. |
| Operations | Passed | Jobs and Scheduler share toolbar, filter, statistic-card, and action-density conventions. |
| Administration | Passed | Data Management, System Status, and Settings use shared spacing and theme tokens without temporary Indigo or white overrides. |
| Sidebar home entry | Passed | The Dashboard row and empty Overview group are absent; the full and compact brand links return to `/admin` and expose a localized accessible name. |
| Responsive layout | Passed | No root horizontal overflow at 1440, 768, or 390px; headings remain below the sticky top bar. |
| Accessibility | Passed | The representative route matrix reports zero automated Axe A/AA violations; keyboard focus, Escape close, focus restore, skip-link focus, and 44px mobile targets pass. |
| Localization | Passed | English and Chinese fixtures render without raw translation keys; bilingual dictionaries remain key-equivalent. |

## Fix history

- P1: Replaced pathname-change focus scrolling with explicit window reset and
  `focus({ preventScroll: true })`.
- P1: Prevented the mobile drawer trigger from reclaiming focus after a route
  navigation while preserving focus restoration for Escape and backdrop
  closes.
- P1: Removed the standalone Dashboard navigation row and converted the entire
  sidebar brand into the semantic home link.
- P2: Added stable page-header and primary-content markers and normalized the
  24px content rhythm across all primary routes.
- P2: Replaced one-off panels, colors, toolbars, filters, and statistic tiles
  with shared primitives and theme tokens where their semantics matched.
- P2: Fixed Creators and Subscriptions segmented controls that exceeded the
  390px viewport.

No open P0, P1, or P2 findings remain.

## Interaction and runtime evidence

- Path navigation: Upload → Tags → Upload resets the viewport and focuses the
  main landmark without hiding the title.
- Query navigation: switching the Jobs task type updates the query without
  changing scroll position.
- Sidebar: desktop, compact, and mobile brand targets return to the Dashboard;
  the mobile drawer closes after navigation.
- Mobile drawer: Escape restores focus to its trigger; navigation leaves focus
  on the destination main landmark.
- Browser console and framework overlay: no application errors or framework
  error overlay in the intercepted route matrix.
- Reduced motion: all primary content remains immediately available.

Validation:

- `npm run check:i18n`
- `npm run typecheck`
- `npm run build`
- complete Playwright suite: 98 passed, 1 documentation screenshot test
  skipped by its explicit environment gate
- `scripts/privacy-scan.sh`
- `git diff --check`

final result: passed
