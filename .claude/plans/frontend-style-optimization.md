# Frontend Style Optimization Plan — auto-gallery Admin Web

**Date:** 2026-06-26
**Scope:** Whole admin-web visual refinement — responsiveness + motion + polish.
**Direction (fixed by brief):** Preserve the current GitHub-grade aesthetic. This
is a *precision refinement*, not a reskin. Complements (does not replace) the
structure/perf items in `.claude/plans/frontend-optimization-audit.md`.

Informed by skills: `frontend-design` (direction, restraint), `make-interfaces-feel-better` (polish details).

---

## 0. Thesis & guardrails

**Thesis.** GitHub's UI is deliberately quiet and dense; its quality comes from
*precision*, not flair. So the win here is precision — consistent tokens, a tuned
responsive system, and a restrained motion language that communicates feedback
and continuity. The "signature" is not a new bold element; it is **GitHub-grade
motion + density discipline applied evenly across every surface.**

**Guardrails (every phase honors these):**
- The GitHub look is unchanged at each step — palette, density, hairline borders, `rounded-md`.
- Quality floor: responsive to 360px, visible `focus-visible`, `prefers-reduced-motion` respected (already in CSS), ≥44px touch targets on coarse pointers.
- **Restraint:** motion serves feedback/continuity only. Over-animation reads as AI-generated. One orchestrated moment beats scattered effects.
- Each phase ships independently with reviewable diffs. No big-bang rewrites.

---

## 1. Current system — what we KEEP (audited)

`globals.css` (234 lines) is already a real design system; we formalize it, we don't rebuild it.

- **Tokens (CSS vars), light + dark:** `--ag-bg/surface/subtle/border/muted/text/accent/success/warning/danger/focus`. Authentic GitHub palette (`#24292f` / `#0969da` / `#d8dee4`; dark `#0d1117`/`#161b22`/`#30363d`/`#58a6ff`).
- **Component classes:** `.card/.card-interactive`, `.btn-primary/ghost/danger/icon`, `.input/.select/.textarea`, `.toolbar`, `.segmented-control/.segment`, `.badge`, `.table-shell/head/row`, `.divider`, `.section-title`.
- **Polish already applied** (make-interfaces-feel-better basics): font-smoothing, `text-wrap: balance/pretty`, `.tabular`, image outlines, `prefers-reduced-motion` reset, 32px min hit, scoped `transition-property` on `.card-interactive`/`.btn-primary`.
- **Motion already present:** `fadeUp` (page-transition), `shimmer` (skeleton), `barGrow`, `.page-item` stagger, `html { transition-colors 300ms }` for theme.

## 2. Gaps (evidence)

| # | Gap | Evidence |
|---|-----|----------|
| G1 | **Tokens not centralized** — `tailwind.config.ts` is `theme:{extend:{}}`; components use **hardcoded hex** instead of `--ag-*`/semantic classes | `tailwind.config.ts:5`; `#24292f`/`#d8dee4` literal across every page |
| G2 | **Responsive is single-breakpoint** — mostly one `md:` flip; thin tablet/large-desktop tuning; no fluid type; per-page container widths | `md:`≈60 vs `sm:`16 / `lg:`18 / `xl:`6 uses; `max-w-4xl`/`max-w-6xl` ad-hoc per page |
| G3 | **Tables don't adapt** — overflow-x scroll on mobile instead of card layout | `.table-shell { overflow-x-auto }` |
| G4 | **No motion system** — durations/easings scattered; no press/exit/overlay/icon-swap primitives; stagger only on `.page-item` | `duration-` 9 uses; overlays (UserMenu/NotificationCenter) toggle instantly |
| G5 | **Polish not systematic** — concentric radius, optical icon alignment, layered overlay shadows, and `tabular-nums` application are inconsistent | `shadow-sm` flat on dropdowns; counters/timers not all tabular |

---

## 3. The plan — 4 phases (P1 → P4, each shippable)

### Phase 1 — Token foundation (enables everything; ~zero visual change)

Make the existing GitHub system *addressable* so responsive + motion + polish can
be applied consistently, not hex-by-hex.

- **`tailwind.config.ts` `theme.extend`:**
  - `colors`: map the CSS vars → semantic names so `bg-white dark:bg-[#161b22]` collapses to `bg-surface`. e.g. `surface: 'var(--ag-surface)'`, `border: 'var(--ag-border)'`, `fg: 'var(--ag-text)'`, `muted: 'var(--ag-muted)'`, `accent: 'var(--ag-accent)'`, plus `success/warning/danger`. (Vars already flip in dark mode → one class, no `dark:` needed.)
  - `borderRadius`: formalize a concentric scale `sm 4 / md 6 / lg 10 / xl 14` (outer = inner + padding).
  - `boxShadow`: `card` (current `shadow-sm`), `overlay` (layered, transparent — for dropdowns/dialogs/popovers), `focus` (the ring).
  - `transitionTimingFunction`: `expo: cubic-bezier(0.16,1,0.3,1)` (already used by fadeUp), `standard: ease-out`.
  - `transitionDuration`: `fast 120 / base 150 / slow 240`.
  - `fontFamily`: `mono: ['JetBrains Mono', …]` (already imported); keep system UI stack for display/body.
  - `fontSize`: a **fluid** display scale using `clamp()` for `h1/h2/section-title` (breathes mobile→4k).
  - `screens`: keep defaults; document intended use (sm=large-phone, md=tablet, lg=laptop, xl/2xl=desktop).
- **Migration is incremental, never big-bang:** new/edited components use semantic tokens; existing hex keeps working. Ship a short `tokens.md` + a hex→token cheat-sheet so swaps happen opportunistically (and during P2–P4 edits).
- **Deliverable:** populated tailwind config + `tokens.md`. **No intended visual change** (verify with before/after screenshots once Docker is back).

### Phase 2 — Responsive system

- **One container primitive:** a `.page` wrapper (`mx-auto w-full max-w-7xl px-4 sm:px-6`) replacing per-page `max-w-4xl/6xl`. Consistent gutters + max line-length.
- **Fluid type:** `clamp()` scale for `h1/h2/.section-title` (from P1) so headings scale smoothly instead of hard jumps.
- **Deliberate tablet + large-desktop tuning** where the single `md:` flip is coarse: toolbars/filter bars (wrap → inline), grids (column counts), settings grid (2→3→4 cols), detail two-pane layouts.
- **Grids:** works/creators grids → `auto-fit` min-card-width (e.g. `repeat(auto-fill, minmax(180px,1fr))`) so columns adapt 2→6 fluidly. (Pairs with the virtualization item in the perf audit.)
- **Tables → cards under `sm`:** jobs/import-jobs/etc. render a stacked card list on phones instead of horizontal scroll (G3). Keep the table at `md+`.
- **Touch targets:** `@media (pointer: coarse)` bumps interactive controls to 44px min; keep 32px desktop density (mouse).
- **Nav:** the mobile hamburger already exists (`layout.tsx`) — refine active state, add `env(safe-area-inset-*)` padding, ensure the wrap-flex desktop nav degrades cleanly at md.

### Phase 3 — Motion language (restraint is the rule)

CSS-first (interruptible, retargets on intent change), scoped `transition-property`, `prefers-reduced-motion` already honored globally.

- **Tokens** from P1 (durations/easings) replace ad-hoc values.
- **Primitives:**
  - **Press:** `active:scale-[0.97]` on buttons/interactive cards (tactile), with a `.no-press` opt-out.
  - **Hover-lift:** `.card-interactive` gains a subtle `translateY(-1px)` + `shadow` on hover (today it changes color only).
  - **List/grid stagger:** extend `.page-item` to works/creators/jobs grids with a **capped** delay (e.g. `min(index*30ms, 300ms)`) so long lists don't crawl.
  - **Overlays** (UserMenu, NotificationCenter, dialogs, toasts): split **enter** (opacity + `translateY(4px)` + `scale(0.98)→1`) / **exit** (shorter ~150ms, quieter). Drive with a `data-state` open/closed pattern.
  - **Icon swaps** (theme toggle, chevron expand/collapse, notification bell): cross-fade opacity+scale, not instant `visibility` toggle.
  - **Skeleton → content:** cross-fade when data arrives (no pop).
  - **One restrained scroll-reveal:** dashboard hero/stat cards only — nowhere else.
- **Hard rules:** never `transition: all`; `will-change` only on `transform/opacity/filter` at known-janky spots; one orchestrated entrance per view, not scattered effects.

### Phase 4 — Per-surface polish pass (make-interfaces-feel-better checklist)

Walk each surface and apply, reporting before/after:

- **Concentric radius:** nested cards/inputs use `outer = inner + padding`.
- **Optical alignment:** nudge asymmetric icons (play ▶, chevrons, arrows) — fix the SVG or a 1px margin.
- **Layered shadows on overlays:** dropdowns/dialogs/popovers move from flat `shadow-sm` → the P1 `shadow-overlay` (transparent, layered).
- **`tabular-nums` everywhere numbers update:** job elapsed timers, stat counters, pagination counts, table numerics (apply `.tabular`).
- **State triad consistency:** every list page uses the same `EmptyState`/loading-skeleton/`ErrorState` pattern, with **action-oriented copy** ("No works yet — start a download" not "Empty"), per frontend-design writing guidance.
- **Focus-visible audit:** custom interactive elements (segments, badge-buttons, thumbnail buttons) all show a visible ring.
- **Image outlines / thumbnails:** already global — verify on the grid thumbnails (they blend into cards otherwise).

---

## 4. Rollout · risk · verification

- **Order:** P1 → P2 → P3 → P4. Each independently shippable; GitHub look preserved at every step.
- **Riskiest = P1 token migration** (touches many files). Mitigate: additive (vars→tailwind), keep hex working, swap opportunistically. Never a single mass rename.
- **Verification (once Docker/WSL is back):** `docker compose build admin-web`; Playwright screenshots at **360 / 768 / 1280 / 1920**, in **light + dark + reduced-motion**; before/after per surface. A picture is worth 1000 tokens.
- **Definition of done per phase:** no visual regression vs the GitHub baseline except the intended refinement; suite/lint green; reduced-motion verified.

## 5. Out of scope (explicitly)

- No new visual identity / no palette change (preserve GitHub).
- No component-library or CSS-framework swap (stay on Tailwind + the existing class system).
- Virtualization / data-fetching / page decomposition → tracked in `frontend-optimization-audit.md` (complementary, not duplicated here).

## 6. Suggested first slice (if approved)

P1 + the two lowest-risk P3 primitives, as one PR:
1. Populate `tailwind.config.ts` from `--ag-*` + add motion/radius/shadow tokens; write `tokens.md`.
2. `active:scale-[0.97]` press + hover-lift on `.card-interactive` (pure CSS, reduced-motion safe).

Zero palette change, immediately tangible "feel" upgrade, fully reversible.
