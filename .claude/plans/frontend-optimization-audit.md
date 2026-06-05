# Frontend Optimization Plan — auto-gallery Admin Web

**Audit date**: 2026-06-01
**Scope**: Frontend only — 36 pages, 13 shared components, ~4000 LoC

---

## Audit Findings

### 1. Page Structure & Component Decomposition

| Finding | Severity | Detail |
|---------|----------|--------|
| Pages too large | HIGH | Works (365L), Jobs (358L), Creators (280L), Subscriptions (257L) — all logic inline |
| Inline components | HIGH | 3-4 `function`/`const` render helpers per page file (GridCard, Elapsed, ActiveIndicator, etc.) |
| No data/presentation separation | MEDIUM | `useQuery` + JSX mixed in same function; no custom hooks for data fetching |
| 13 shared components | MEDIUM | Missing: FilterBar, BatchActionBar, PaginationFooter |
| Layout consistency | LOW | Each page re-implements its own toolbar/filter bar layout |

### 2. Image Grid Performance

| Finding | Severity | Detail |
|---------|----------|--------|
| No virtualization | HIGH | Grid renders ALL items — 1000+ works will cause layout thrashing |
| `React.memo` on GridCard | OK | Recently added |
| `loading="lazy"` on thumbnails | OK | Native lazy loading present |
| No IntersectionObserver | MEDIUM | Above-fold images load eagerly; no priority hints |
| No srcSet/responsive images | MEDIUM | Same thumbnail URL for all viewport sizes |
| Asset preloading absent | LOW | No `<link rel="preload">` for next-page images |

### 3. Filter & Search Interactions

| Finding | Severity | Detail |
|---------|----------|--------|
| URL-based filter state | OK | All filters persisted in searchParams |
| No "clear all filters" | HIGH | 6 filter dimensions, no single reset |
| No active filter indicator | MEDIUM | User can't see which filters are active without reading each control |
| Filter bar overcrowded | MEDIUM | 7 controls in single row (search + source + creator + NSFW + fav + AI + sort) |
| Sort direction toggle | OK | Recently implemented |
| Debounced search | OK | 300ms debounce on search input |

### 4. Batch Selection

| Finding | Severity | Detail |
|---------|----------|--------|
| Works page: NO batch selection | CRITICAL | Most content-heavy page has zero multi-select |
| Jobs page: full batch | OK | Checkboxes + select all + action bar |
| Creators page: partial | MEDIUM | Multi-select exists but no select-all, no batch action bar |
| No batch operations on works | HIGH | Can't batch delete, batch tag, or batch export works |
| No keyboard multi-select | LOW | No Shift+click range selection |

### 5. Image Preview / Viewer

| Finding | Severity | Detail |
|---------|----------|--------|
| No fullscreen/lightbox | HIGH | Preview confined to card; no immersive viewing |
| No keyboard navigation | HIGH | Arrow keys don't navigate pages |
| No pinch-to-zoom | MEDIUM | Mobile viewing lacks standard gallery gestures |
| Page thumbnails work | OK | Horizontal scrollable strip below preview |
| No swipe gesture | MEDIUM | Mobile swipe for prev/next page not implemented |

### 6. Mobile Adaptation

| Finding | Severity | Detail |
|---------|----------|--------|
| Grid columns responsive | OK | 2 cols -> 4 -> 5 at breakpoints |
| Filter bar doesn't collapse | HIGH | 7 controls overflow on <768px |
| Jobs table breaks | HIGH | Horizontal scroll missing on narrow viewports |
| No mobile navigation drawer | MEDIUM | Sidebar nav hardcoded for desktop |
| Touch targets small | MEDIUM | Filter buttons <40px on mobile |

### 7. Loading, Empty, Error States

| Finding | Severity | Detail |
|---------|----------|--------|
| Loading skeletons | GOOD | 77 occurrences, consistent animate-pulse patterns |
| EmptyState component | GOOD | Used across all list pages |
| ErrorState + ErrorBoundary | GOOD | Global boundary + per-component error states |
| No optimistic updates on works | MEDIUM | Favorite toggle refreshes entire query |

### 8. Accessibility

| Finding | Severity | Detail |
|---------|----------|--------|
| Images have alt text | OK | Dynamic alt from title or filename |
| No ARIA labels | HIGH | Interactive controls lack `aria-label` |
| No skip-to-content link | HIGH | Keyboard users must tab through entire nav |
| No keyboard grid navigation | MEDIUM | Arrow keys don't navigate between grid cards |
| No focus trap in modals | MEDIUM | Tab can escape modal to background |
| No screen reader announcements | MEDIUM | Filter changes, page loads not announced |

---

## Phased Optimization Plan

### Phase 1: Critical Fixes (accessibility + batch selection)
**Effort**: 2-3 hours | **Impact**: HIGH

| # | Task | File(s) |
|---|------|---------|
| 1.1 | Add skip-to-content link and `aria-label` on all icon buttons | `layout.tsx`, all pages |
| 1.2 | Add batch selection to Works page (checkboxes + select all + action bar) | `works/page.tsx` |
| 1.3 | Add "Clear all filters" button to Works page filter bar | `works/page.tsx` |
| 1.4 | Add `aria-label` to filter controls, grid cards, action buttons | `works/page.tsx` |
| 1.5 | Add focus trap to Modal component | `Modal.tsx` |

### Phase 2: Performance (image grid + mobile)
**Effort**: 3-4 hours | **Impact**: HIGH

| # | Task | File(s) |
|---|------|---------|
| 2.1 | Integrate `@tanstack/react-virtual` for works grid/list | `works/page.tsx` |
| 2.2 | Add IntersectionObserver lazy boundary for below-fold images | `works/page.tsx` |
| 2.3 | Collapse filter bar on mobile into a dropdown/drawer | `works/page.tsx` |
| 2.4 | Fix Jobs page horizontal scroll on mobile | `jobs/page.tsx` |
| 2.5 | Extract `PaginationFooter` shared component | New `PaginationFooter.tsx` |

### Phase 3: Component Extraction (code quality)
**Effort**: 2-3 hours | **Impact**: MEDIUM

| # | Task | File(s) |
|---|------|---------|
| 3.1 | Extract `FilterBar` component from Works page | New `FilterBar.tsx` |
| 3.2 | Extract `JobCard` from Jobs page | New `JobCard.tsx` |
| 3.3 | Extract `CreatorCard` from Creators page | New `CreatorCard.tsx` |
| 3.4 | Extract `useWorksFilter` hook (URL params <-> filter state) | New `useWorksFilter.ts` |
| 3.5 | Add `activeFilterCount` badge to FilterBar | `FilterBar.tsx` |

### Phase 4: Viewer Enhancement (preview + navigation)
**Effort**: 3-4 hours | **Impact**: MEDIUM

| # | Task | File(s) |
|---|------|---------|
| 4.1 | Add keyboard navigation to work detail (arrows for prev/next page) | `works/[id]/page.tsx` |
| 4.2 | Add fullscreen/lightbox mode for image preview | `works/[id]/page.tsx` |
| 4.3 | Add swipe gesture for page navigation on mobile | `works/[id]/page.tsx` |
| 4.4 | Add Shift+click range selection to batch mode | `works/page.tsx` |
| 4.5 | Add download button for original asset | `works/[id]/page.tsx` |

### Phase 5: Polish (mobile + UX)
**Effort**: 2-3 hours | **Impact**: LOW-MEDIUM

| # | Task | File(s) |
|---|------|---------|
| 5.1 | Add mobile hamburger nav drawer | `layout.tsx` |
| 5.2 | Add screen reader live-region for filter changes | `works/page.tsx` |
| 5.3 | Add optimistic update for work favorite toggle | `works/page.tsx` |
| 5.4 | Add retry countdown to ErrorState | `ErrorState.tsx` |
| 5.5 | Extract all page-local inline functions to top-level | Multiple |

---

**Total estimated effort**: 12-17 hours across 5 phases.
**Priority order**: Phase 1 -> 2 -> 3 -> 4 -> 5, each phase independently deployable.
**110 tests must continue to pass after each phase.**
