# Frontend UX Closed-Loop Navigation — Design

**Goal:** Close navigation gaps between Works, Creators, Tags, and Search so users can navigate bidirectionally between all entity types without dead ends.

**Architecture:** Four modules — (1) Navigation closed loops (frontend-only Link replacements), (2) Meilisearch multi-entity search (backend indexing + frontend tabs), (3) Tag detail page (new), (4) Breadcrumb component (new). Maximum reuse of existing API shape; new backend work limited to search indexing and one tag detail endpoint.

**Tech Stack:** Next.js 14, React 18, TypeScript 5, TanStack Query 5, Tailwind CSS; FastAPI + Meilisearch (backend)

---

## Module 1: Navigation Closed Loops (Frontend-Only)

### 1.1 Current State

| From | To | Status |
|------|-----|--------|
| Work card/list → Creator | `creator_name` is plain `<span>` | ❌ |
| Work detail → Creator | `source_creator_id` is plain text, creator name not shown in header | ❌ |
| Work detail → "More from creator" | No section exists | ❌ |
| Search result → Creator | Creator name is plain `<span>` | ❌ |
| Tag chip → Tag detail | Goes to `/admin/search?q=tag`, no standalone tag page | ⚠️ |
| Source badge → Source filter | `<SourceBadge>` is decorative `<span>` | ❌ |

### 1.2 Changes

#### Works list — GridCard (works/page.tsx:103)

```tsx
// Before:
{w.creator_name && <span className="text-xs text-gray-400 ...">{w.creator_name}</span>}

// After:
{w.creator_name && w.creator_id && (
  <Link href={`/admin/creators/${w.creator_id}`}
    className="text-xs text-blue-600 hover:underline truncate"
    onClick={(e) => e.stopPropagation()}>
    {w.creator_name}
  </Link>
)}
```

Same pattern applied to list view (works/page.tsx:538) and search results (search/page.tsx:86).

#### Work detail — header (works/[id]/page.tsx:279-296)

Add creator name link to the PageHeader description area. Ensure the work detail API response includes `creator_id` and `creator_name`.

#### Work detail — "More from this creator" section

New section in right column, below assets: fetches `listWorks({ creator_id, limit: 6, sort_by: posted_at })`, displays mini grid excluding current work, with "View all" link to `/admin/works?creator=${id}`.

#### SourceBadge clickable

Wrap SourceBadge usage in Link to `/admin/works?source=${source}` with `e.stopPropagation()`. Applied in: GridCard, list view, search results, work detail header.

---

## Module 2: Search Enhancement (Multi-Entity Meilisearch)

### 2.1 Backend — Index Creators and Tags

Add two Meilisearch indexes alongside the existing `works` index.

**creators index schema:**
```json
{
  "id": "uuid",
  "name": "ASK",
  "display_name": "ASK",
  "description": "...",
  "source_breakdown": [{"source": "pixiv", "count": 203}],
  "total_works": 341
}
```

**tags index schema:**
```json
{
  "id": "uuid",
  "normalized_name": "original",
  "category": "general",
  "work_count": 341
}
```

**Index triggers:**
- Creator created/updated → `SearchService.index_creator(creator)`
- Tag created/merged → `SearchService.index_tag(tag)`
- Full reindex (`POST /api/v1/search/reindex`) clears and rebuilds all three indexes

**Files:**
- Modify: `backend/app/services/search.py` — add `index_creator()`, `index_tag()`, extend `search()` with `kind` parameter
- Modify: `backend/app/api/search.py` — accept `kind` query parameter
- Modify: `backend/app/services/creator.py` — call `index_creator()` on create/update
- Modify: `backend/app/services/tag.py` — call `index_tag()` on create/merge

### 2.2 API Extension

`GET /api/v1/search` extended parameters:

| Param | Type | Default | Values |
|-------|------|---------|--------|
| `q` | string | (required) | Search query |
| `kind` | string | `all` | `all`, `works`, `creators`, `tags` |
| `limit` | int | 20 | 1-100 |
| `offset` | int | 0 | >=0 |

**Response (kind=all):**
```json
{
  "query": "ASK",
  "total": 42,
  "works": {
    "total": 30,
    "hits": [{ "id": "...", "title": "...", "creator_name": "ASK", "thumbnail_asset_id": "...", "source": "pixiv" }]
  },
  "creators": {
    "total": 1,
    "hits": [{ "id": "...", "name": "ASK", "display_name": "ASK", "total_works": 341 }]
  },
  "tags": {
    "total": 11,
    "hits": [{ "id": "...", "normalized_name": "original", "category": "general", "work_count": 341 }]
  }
}
```

When `kind` is specific (e.g., `creators`), only that section is returned.

### 2.3 Frontend — Search Page Tabs

Search page redesigned with tabs: 全部 / 作品 / 画师 / 标签.

Each result type has a distinct card:
- **Work**: thumbnail + title + creator link + source badge + tags
- **Creator**: initials avatar + name + work count + source breakdown (mini bar)
- **Tag**: tag chip + category + work count

All cross-entity references (creator name, tag chips, source badge) are clickable Links.

---

## Module 3: Tag Detail Page (New)

### 3.1 Route

`/admin/tags/[id]` — new Next.js page at `admin-web/src/app/admin/tags/[id]/page.tsx`.

### 3.2 API

`GET /api/v1/tags/{id}` returns:

```json
{
  "id": "uuid",
  "normalized_name": "original",
  "category": "general",
  "aliases": [],
  "work_count": 341,
  "top_creators": [
    { "creator_id": "uuid", "creator_name": "ASK", "work_count": 203 }
  ]
}
```

`GET /api/v1/works?tag_id={id}` — filter works by tag (confirm or add backend support).

### 3.3 Page Layout

Two-column: left sidebar (tag info, aliases, top creators), main content (works grid with pagination). Reuses existing WorkGrid component.

### 3.4 Entry Points

Tag chips across all pages link to `/admin/tags/[id]` instead of `/admin/search?q=...`. Tags list page names become `<Link>` elements. Add `work_count` column to tags list, default sort descending.

---

## Module 4: Breadcrumb Component (New)

### 4.1 Component

`admin-web/src/components/Breadcrumb.tsx` — renders `crumb[]` array as `<nav>` with clickable Links. Each crumb: `{ label: string; href?: string }`. Omitted `href` = current page (last crumb, non-clickable).

### 4.2 Breadcrumb Configurations

| Page | Crumbs |
|------|--------|
| `/admin/works` | `[{ label: "作品" }]` |
| `/admin/works/[id]` | `[{ label: "作品", href: "/admin/works" }, { label: creatorName, href: "/admin/creators/${id}" }, { label: workTitle }]` |
| `/admin/creators` | `[{ label: "画师" }]` |
| `/admin/creators/[id]` | `[{ label: "画师", href: "/admin/creators" }, { label: creatorName }]` |
| `/admin/tags` | `[{ label: "标签" }]` |
| `/admin/tags/[id]` | `[{ label: "标签", href: "/admin/tags" }, { label: tagName }]` |
| `/admin/search?q=xxx` | `[{ label: "搜索" }, { label: "xxx" }]` |

---

## Files Summary

### Frontend — New

| File | Purpose |
|------|---------|
| `admin-web/src/components/Breadcrumb.tsx` | Breadcrumb component |
| `admin-web/src/app/admin/tags/[id]/page.tsx` | Tag detail page |

### Frontend — Modified

| File | Changes |
|------|---------|
| `admin-web/src/app/admin/works/page.tsx` | Creator name → Link, SourceBadge → Link |
| `admin-web/src/app/admin/works/[id]/page.tsx` | Creator header link, MoreFromCreator, tag→detail, breadcrumb |
| `admin-web/src/app/admin/search/page.tsx` | Multi-tab redesign, all entity links, breadcrumb |
| `admin-web/src/app/admin/creators/[id]/page.tsx` | Tag buttons → detail, breadcrumb |
| `admin-web/src/app/admin/tags/page.tsx` | Names → Links, work_count column |
| `admin-web/src/components/SourceBadge.tsx` | Optional href/onClick prop |
| `admin-web/src/lib/i18n.tsx` | ~20 new keys (zh + en) |
| `admin-web/src/lib/api.ts` | `getTag()`, typed search methods |
| `admin-web/src/lib/api/types.ts` | TagDetail, multi-entity search types |

### Backend — Modified

| File | Changes |
|------|---------|
| `backend/app/services/search.py` | `index_creator()`, `index_tag()`, `search(kind)` |
| `backend/app/api/search.py` | `kind` query param |
| `backend/app/api/tags.py` | `GET /tags/{id}` detail endpoint |
| `backend/app/services/creator.py` | Meili sync on create/update |
| `backend/app/services/tag.py` | Meili sync on create/merge |
| `backend/app/schemas/tag.py` | TagDetail schema |

---

## Constraints

- All navigation uses Next.js `<Link>` (no full-page reloads)
- Tag detail page reuses existing `WorkGrid` component
- Breadcrumb is SSR-safe (props-driven, no `useSearchParams`)
- Backend search is backward-compatible (`kind` defaults to `all`)
- All new UI strings have both `zh` and `en` i18n entries
- No new npm/pip dependencies
