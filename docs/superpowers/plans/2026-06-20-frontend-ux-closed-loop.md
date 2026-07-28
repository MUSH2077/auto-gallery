# Frontend UX Closed-Loop Navigation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close navigation gaps so users can navigate bidirectionally between Works, Creators, Tags, and Search — every entity name is a clickable link, search covers all entity types, and breadcrumbs provide traceability.

**Architecture:** Backend changes extend 3 existing endpoints (WorkRead schema, search `kind` param, tag detail `top_creators`). Frontend adds 1 new component (Breadcrumb), 1 new page (tags/[id]), and modifies 6 existing pages to replace plain `<span>` text with Next.js `<Link>` components. The SearchService already indexes all 3 entities — frontend just doesn't consume the data.

**Tech Stack:** Next.js 14 + React 18 + TypeScript 5 + TanStack Query 5 + Tailwind CSS; FastAPI + SQLAlchemy 2.0 async + Meilisearch

## Global Constraints

- All navigation must use Next.js `<Link>` for client-side routing (no full-page reloads)
- Tag detail page reuses existing `WorkGrid` component
- Breadcrumb component must be SSR-safe (props-driven, no `useSearchParams`)
- Backend search changes must be backward-compatible (`kind` defaults to `all`)
- All new UI strings must have both `zh` and `en` i18n entries
- No new npm/pip dependencies
- Follow existing code patterns: thin pages, TanStack Query for server state, business logic in services

---

### Task 1: Backend — WorkRead schema + creator join

**Files:**
- Modify: `backend/app/schemas/work.py`
- Modify: `backend/app/repositories/work.py`

**Interfaces:**
- Produces: `WorkRead` now includes `creator_id: UUID | None`, `creator_name: str | None`
- Consumed by: Task 6 (work detail frontend)

- [ ] **Step 1: Add creator fields to WorkRead schema**

In `backend/app/schemas/work.py`, add two fields to `WorkRead`:

```python
class WorkRead(BaseModel):
    id: UUID
    title: str | None = None
    description: str | None = None
    posted_at: datetime | None = None
    thumbnail_asset_id: UUID | None = None
    asset_count: int = 1
    is_nsfw: bool
    is_ai_generated: bool = False
    is_favorite: bool
    creator_id: UUID | None = None          # NEW
    creator_name: str | None = None         # NEW
    curation_state: CurationStateRead | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
```

- [ ] **Step 2: Update WorkRepository.get() to join creator**

In `backend/app/repositories/work.py`, modify the `get()` method:

```python
async def get(self, work_id: UUID) -> Work | None:
    work = await self.session.get(Work, work_id)
    if work is None:
        return None
    from app.models.work_source import WorkSource
    from app.models.source_creator import SourceCreator
    from app.models.creator import Creator
    row = await self.session.execute(
        select(Creator.id, Creator.display_name, Creator.name)
        .join(SourceCreator, SourceCreator.creator_id == Creator.id)
        .join(WorkSource, WorkSource.source_creator_id == SourceCreator.source_creator_id)
        .where(WorkSource.work_id == work_id)
        .limit(1)
    )
    result = row.one_or_none()
    if result:
        work.creator_id = result[0]
        work.creator_name = result[1] or result[2]
    return work
```

- [ ] **Step 3: Verify with curl**

```bash
docker compose up -d --force-recreate backend
sleep 10
WORK_ID=$(docker compose exec postgres psql -U autogallery -t -c "SELECT id FROM works LIMIT 1;" | tr -d ' ')
curl -s -H "Authorization: Bearer <token>" "http://localhost:8818/api/v1/works/${WORK_ID}" | python3 -c "import sys,json; d=json.load(sys.stdin); print('creator_id:', d.get('creator_id'), 'creator_name:', d.get('creator_name'))"
```

Expected: `creator_id: <uuid> creator_name: <name>`

- [ ] **Step 4: Commit**

```bash
git add backend/app/schemas/work.py backend/app/repositories/work.py
git commit -m "feat: add creator_id and creator_name to WorkRead schema"
```

---

### Task 2: Backend — SearchService kind param + incremental indexing

**Files:**
- Modify: `backend/app/services/search.py`
- Modify: `backend/app/api/search.py`

**Interfaces:**
- Produces: `SearchService.search(q, offset, limit, kind="all")`, `index_creator()`, `index_tag()`, `delete_creator()`, `delete_tag()`
- Consumed by: Task 7 (search frontend)

- [ ] **Step 1: Add incremental index methods**

In `backend/app/services/search.py`, add after `index_work()` (after line ~106):

```python
async def index_creator(self, creator_id: str, name: str, display_name: str | None,
                        description: str | None, is_active: bool, created_at: str):
    try:
        client = _client()
        _ensure_indexes(client)
        client.index(CREATORS_INDEX).add_documents([{
            "id": creator_id, "name": name,
            "display_name": display_name or name,
            "description": (description or "")[:500],
            "is_active": is_active, "created_at": created_at,
        }])
    except Exception as e:
        logger.warning("Failed to index creator %s: %s", creator_id, e)

async def index_tag(self, tag_id: str, normalized_name: str, category: str | None, created_at: str):
    try:
        client = _client()
        _ensure_indexes(client)
        client.index(TAGS_INDEX).add_documents([{
            "id": tag_id, "normalized_name": normalized_name,
            "category": category or "general", "created_at": created_at,
        }])
    except Exception as e:
        logger.warning("Failed to index tag %s: %s", tag_id, e)

async def delete_creator(self, creator_id: str):
    try:
        client = _client()
        client.index(CREATORS_INDEX).delete_document(creator_id)
    except Exception as e:
        logger.debug("Failed to delete creator %s from index: %s", creator_id, e)

async def delete_tag(self, tag_id: str):
    try:
        client = _client()
        client.index(TAGS_INDEX).delete_document(tag_id)
    except Exception as e:
        logger.debug("Failed to delete tag %s from index: %s", tag_id, e)
```

- [ ] **Step 2: Add kind parameter to search()**

Modify existing `search()` signature and body:

```python
async def search(self, query: str, offset: int = 0, limit: int = 20, kind: str = "all") -> dict:
    try:
        client = _client()
        result = {"query": query, "total": 0, "results": [], "creators": [], "tags": []}

        if kind in ("all", "works"):
            works_result = client.index(WORKS_INDEX).search(query, offset=offset, limit=limit)
            result["results"] = list(getattr(works_result, 'hits', []) or [])
            result["total"] = getattr(works_result, 'estimated_total_hits', 0) or 0

        if kind in ("all", "creators"):
            try:
                cr = client.index(CREATORS_INDEX).search(query, limit=10 if kind == "all" else limit)
                result["creators"] = list(getattr(cr, 'hits', []) or [])
                if kind == "creators":
                    result["total"] = getattr(cr, 'estimated_total_hits', 0) or 0
            except Exception:
                pass

        if kind in ("all", "tags"):
            try:
                tr = client.index(TAGS_INDEX).search(query, limit=10 if kind == "all" else limit)
                result["tags"] = list(getattr(tr, 'hits', []) or [])
                if kind == "tags":
                    result["total"] = getattr(tr, 'estimated_total_hits', 0) or 0
            except Exception:
                pass

        return result
    except Exception as e:
        logger.warning("Meilisearch search failed: %s", e)
        return {"results": [], "total": 0, "query": query, "creators": [], "tags": []}
```

- [ ] **Step 3: Update search API route**

In `backend/app/api/search.py`:

```python
@router.get("")
async def search(
    q: str = Query("", description="Search query"),
    kind: str = Query("all", description="Entity type: all, works, creators, tags"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    svc = SearchService(db)
    return await svc.search(q, offset, limit, kind=kind)
```

- [ ] **Step 4: Verify**

```bash
docker compose up -d --force-recreate backend
sleep 10
curl -s -H "Authorization: Bearer <token>" "http://localhost:8818/api/v1/search?q=ASK&kind=creators" | python3 -c "import sys,json; d=json.load(sys.stdin); print('creators:', len(d.get('creators',[])))"
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/search.py backend/app/api/search.py
git commit -m "feat: add kind param to search, incremental creator/tag indexing"
```

---

### Task 3: Backend — Tag detail with top_creators

**Files:**
- Modify: `backend/app/schemas/tag.py`
- Modify: `backend/app/api/tags.py`

**Interfaces:**
- Produces: `GET /api/v1/tags/{tag_id}` returns `TagDetail` with `top_creators`
- Consumed by: Task 8 (tag detail page)

- [ ] **Step 1: Add TagDetail and CreatorRef schemas**

In `backend/app/schemas/tag.py`:

```python
from pydantic import BaseModel
from uuid import UUID

class CreatorRef(BaseModel):
    creator_id: UUID
    creator_name: str
    work_count: int

class TagDetail(TagRead):
    top_creators: list[CreatorRef] = []
```

- [ ] **Step 2: Update get_tag endpoint**

In `backend/app/api/tags.py`, modify `get_tag()`:

```python
from app.schemas.tag import TagDetail

@router.get("/{tag_id}", response_model=TagDetail)
async def get_tag(tag_id: UUID, db: AsyncSession = Depends(get_db)):
    repo = TagRepository(db)
    tag = await repo.get(tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")

    from app.models.work_tag import WorkTag
    from app.models.work_source import WorkSource
    from app.models.source_creator import SourceCreator
    from app.models.creator import Creator
    from sqlalchemy import func

    top_creators_rows = await db.execute(
        select(
            Creator.id,
            func.coalesce(Creator.display_name, Creator.name).label("creator_name"),
            func.count(WorkTag.work_id).label("work_count"),
        )
        .join(SourceCreator, SourceCreator.creator_id == Creator.id)
        .join(WorkSource, WorkSource.source_creator_id == SourceCreator.source_creator_id)
        .join(WorkTag, WorkTag.work_id == WorkSource.work_id)
        .where(WorkTag.tag_id == tag_id)
        .group_by(Creator.id)
        .order_by(func.count(WorkTag.work_id).desc())
        .limit(10)
    )
    top_creators = [
        CreatorRef(creator_id=r[0], creator_name=str(r[1]), work_count=r[2])
        for r in top_creators_rows.all()
    ]

    return TagDetail(
        id=tag.id,
        normalized_name=tag.normalized_name,
        category=tag.category,
        usage_count=tag.usage_count,
        created_at=tag.created_at,
        top_creators=top_creators,
    )
```

- [ ] **Step 3: Verify**

```bash
docker compose up -d --force-recreate backend
sleep 10
TAG_ID=$(docker compose exec postgres psql -U autogallery -t -c "SELECT id FROM tags LIMIT 1;" | tr -d ' ')
curl -s -H "Authorization: Bearer <token>" "http://localhost:8818/api/v1/tags/${TAG_ID}" | python3 -c "import sys,json; d=json.load(sys.stdin); print('creators:', len(d.get('top_creators',[])))"
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/schemas/tag.py backend/app/api/tags.py
git commit -m "feat: add top_creators to tag detail endpoint"
```

---

### Task 4: Frontend — Breadcrumb component

**Files:**
- Create: `admin-web/src/components/Breadcrumb.tsx`
- Modify: `admin-web/src/components/index.ts`

- [ ] **Step 1: Write the component**

Create `admin-web/src/components/Breadcrumb.tsx`:

```tsx
"use client";
import Link from "next/link";

export type Crumb = { label: string; href?: string };

export function Breadcrumb({ items }: { items: Crumb[] }) {
  return (
    <nav aria-label="Breadcrumb" className="flex items-center gap-1.5 text-sm text-gray-500 dark:text-gray-400 mb-4">
      {items.map((item, i) => (
        <span key={i} className="flex items-center gap-1.5">
          {i > 0 && <span className="text-gray-300 dark:text-gray-600" aria-hidden="true">/</span>}
          {item.href ? (
            <Link href={item.href} className="hover:text-blue-600 dark:hover:text-blue-400 hover:underline transition-colors">
              {item.label}
            </Link>
          ) : (
            <span className="text-gray-900 dark:text-white font-medium">{item.label}</span>
          )}
        </span>
      ))}
    </nav>
  );
}
```

- [ ] **Step 2: Export from barrel**

In `admin-web/src/components/index.ts`, add:
```ts
export { Breadcrumb, type Crumb } from "./Breadcrumb";
```

- [ ] **Step 3: Verify build**

```bash
cd admin-web && npm run typecheck 2>&1 | tail -5
```

Expected: no new errors.

- [ ] **Step 4: Commit**

```bash
git add admin-web/src/components/Breadcrumb.tsx admin-web/src/components/index.ts
git commit -m "feat: add Breadcrumb navigation component"
```

---

### Task 5: Frontend — Works list: creator → Link, SourceBadge → Link

**Files:**
- Modify: `admin-web/src/app/admin/works/page.tsx` (~lines 101-103, 536-540)
- Modify: `admin-web/src/components/SourceBadge.tsx`

- [ ] **Step 1: Make SourceBadge optionally clickable**

Modify `admin-web/src/components/SourceBadge.tsx`:

```tsx
import Link from "next/link";
import { getSourceBadgeColor } from "@/lib/sourceColors";

export default function SourceBadge({ source, href }: { source: string; href?: string }) {
  const badge = (
    <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${getSourceBadgeColor(source)}`}>
      {source}
    </span>
  );
  if (href) {
    return <Link href={href} onClick={(e) => e.stopPropagation()} className="inline-flex">{badge}</Link>;
  }
  return badge;
}
```

- [ ] **Step 2: Update GridCard — creator name Link**

In `works/page.tsx`, find line ~103 with `{w.creator_name && <span ...>...}` and replace:

```tsx
{w.creator_name && w.creator_id && (
  <Link href={`/admin/creators/${w.creator_id}`}
    className="text-xs text-blue-600 hover:underline truncate"
    onClick={(e) => e.stopPropagation()}>
    {w.creator_name}
  </Link>
)}
```

Add `import Link from "next/link";` at the top of the file if not already present.

- [ ] **Step 3: Update SourceBadge usage in GridCard + list view**

Replace `<SourceBadge source={w.source} />` with:
```tsx
<SourceBadge source={w.source} href={`/admin/works?source=${w.source}`} />
```

Found in GridCard (~line 101) and list view row (~line 537).

- [ ] **Step 4: Update list view creator name**

Same change as Step 2 applied to the list view row (~line 538).

- [ ] **Step 5: Commit**

```bash
git add admin-web/src/app/admin/works/page.tsx admin-web/src/components/SourceBadge.tsx
git commit -m "feat: make creator names and source badges clickable on works list"
```

---

### Task 6: Frontend — Work detail: creator link, MoreFromCreator, breadcrumb

**Files:**
- Modify: `admin-web/src/app/admin/works/[id]/page.tsx`

- [ ] **Step 1: Add imports**

At top of file, ensure `Link` is imported from `next/link`. Add:
```tsx
import { Breadcrumb } from "@/components/Breadcrumb";
```

- [ ] **Step 2: Add Breadcrumb**

After the `<main>` opening line (~277), insert:

```tsx
<Breadcrumb items={[
  { label: t("works.title"), href: "/admin/works" },
  ...(w.creator_name && w.creator_id ? [{ label: w.creator_name, href: `/admin/creators/${w.creator_id}` }] : []),
  { label: w.title || t("work_detail.untitled") },
]} />
```

- [ ] **Step 3: Add creator link in header**

In the PageHeader `description` area, after the `<SourceBadge>` line, add:

```tsx
{w.creator_name && w.creator_id && (
  <span className="text-sm">
    {t("work_detail.by")}{" "}
    <Link href={`/admin/creators/${w.creator_id}`}
      className="text-blue-600 hover:underline font-medium">
      {w.creator_name}
    </Link>
  </span>
)}
```

- [ ] **Step 4: Change tag chip links to tag detail**

Lines ~372-373 (normalized tags): change `router.push(\`/admin/search?q=...\`)` to `router.push(\`/admin/tags/${t.id}\`)`.

Lines ~385-387 (source tags): keep search link for tags without DB IDs.

- [ ] **Step 5: Add MoreFromCreator component**

Add new function before `export default`:

```tsx
function MoreFromCreator({ creatorId, currentWorkId }: { creatorId: string; currentWorkId: string }) {
  const t = useT();
  const works = useQuery({
    queryKey: ["more-from-creator", creatorId],
    queryFn: () => api.listWorks(0, 6, { creator_id: creatorId, sort_by: "posted_at", sort_order: "desc" }),
  });
  if (works.isLoading || !works.data?.items?.length) return null;
  const others = works.data.items.filter(w => w.id !== currentWorkId).slice(0, 5);
  if (!others.length) return null;
  return (
    <div className="card p-4">
      <h3 className="font-medium mb-3 text-sm">{t("work_detail.more_from_creator")}</h3>
      <div className="grid grid-cols-2 gap-2">
        {others.map(w => (
          <Link key={w.id} href={`/admin/works/${w.id}`} className="group">
            <div className="aspect-[4/3] bg-gray-100 dark:bg-slate-700 rounded overflow-hidden">
              {w.thumbnail_asset_id ? (
                <img src={api.mediaUrl(w.thumbnail_asset_id, "thumb")} alt={w.title || ""} className="w-full h-full object-cover" loading="lazy" />
              ) : (
                <div className="flex h-full items-center justify-center text-xs text-gray-400">{t("works.na")}</div>
              )}
            </div>
            <p className="text-xs mt-1 truncate group-hover:text-blue-600">{w.title || t("works.untitled")}</p>
          </Link>
        ))}
      </div>
      <Link href={`/admin/works?creator=${creatorId}`} className="text-xs text-blue-600 hover:underline mt-2 inline-block">
        {t("work_detail.view_all_from_creator")}
      </Link>
    </div>
  );
}
```

Render in right column after `<AllPages>`:
```tsx
{w.creator_id && <MoreFromCreator creatorId={w.creator_id} currentWorkId={id} />}
```

- [ ] **Step 6: Commit**

```bash
git add admin-web/src/app/admin/works/[id]/page.tsx
git commit -m "feat: breadcrumb, creator link, more-from-creator on work detail"
```

---

### Task 7: Frontend — Search page multi-tab redesign

**Files:**
- Modify: `admin-web/src/app/admin/search/page.tsx`
- Modify: `admin-web/src/lib/api.ts`
- Modify: `admin-web/src/lib/api/types.ts`

- [ ] **Step 1: Add types**

In `admin-web/src/lib/api/types.ts`, add:

```typescript
export interface CreatorSearchHit {
  id: string; name: string; display_name: string; description?: string;
  is_active: boolean; created_at: string;
}
export interface TagSearchHit {
  id: string; normalized_name: string; category?: string; created_at: string;
}
export interface SearchResponse {
  query: string; total: number; results?: WorkListItem[];
  creators?: CreatorSearchHit[]; tags?: TagSearchHit[];
}
```

- [ ] **Step 2: Update api.search() signature**

In `admin-web/src/lib/api.ts`, update `search`:

```typescript
async search(query: string, offset = 0, limit = 20, kind = "all"): Promise<SearchResponse> {
  const params = new URLSearchParams({ q: query, offset: String(offset), limit: String(limit) });
  if (kind !== "all") params.set("kind", kind);
  return this.get(`/api/v1/search?${params.toString()}`);
}
```

- [ ] **Step 3: Rewrite SearchContent component**

Replace the contents of `SearchContent` in `search/page.tsx` with a version that includes:
- `kind` state (`"all" | "works" | "creators" | "tags"`)
- Tab bar with counts
- Creator card: initials avatar + name + description
- Work card: same as before but with clickable creator name and SourceBadge href
- Tag card: `#tagname` chip linking to `/admin/tags/{id}`
- Breadcrumb: `搜索 / "query"`

(See design doc Module 2.3 and spec Module 2 for the complete layout.)

- [ ] **Step 4: Verify build**

```bash
cd admin-web && npm run typecheck 2>&1 | tail -10
```

Expected: no new errors.

- [ ] **Step 5: Commit**

```bash
git add admin-web/src/app/admin/search/page.tsx admin-web/src/lib/api.ts admin-web/src/lib/api/types.ts
git commit -m "feat: multi-tab search with creators and tags"
```

---

### Task 8: Frontend — Tag detail page (new)

**Files:**
- Create: `admin-web/src/app/admin/tags/[id]/page.tsx`

- [ ] **Step 1: Add API types and method**

In `admin-web/src/lib/api/types.ts`:
```typescript
export interface CreatorRef { creator_id: string; creator_name: string; work_count: number; }
export interface TagDetail {
  id: string; normalized_name: string; category?: string;
  usage_count: number; top_creators: CreatorRef[]; created_at: string;
}
```

In `admin-web/src/lib/api.ts`:
```typescript
async getTagDetail(id: string): Promise<TagDetail> {
  return this.get(`/api/v1/tags/${id}`);
}
```

- [ ] **Step 2: Create the page**

Create `admin-web/src/app/admin/tags/[id]/page.tsx` with:
- Breadcrumb: `标签 / #tagname`
- Left sidebar: tag name, category badge, usage count, created date, top creators list (each clickable)
- Main section: works grid (paginated, 24 per page, `listWorks({ tag: normalizedName })`)
- Mini work cards: thumbnail + title + creator name

(See design doc Module 3.3 and spec Module 3 for the complete layout.)

- [ ] **Step 3: Verify build**

```bash
cd admin-web && npm run typecheck 2>&1 | tail -10
```

- [ ] **Step 4: Commit**

```bash
git add admin-web/src/app/admin/tags/[id]/page.tsx admin-web/src/lib/api.ts admin-web/src/lib/api/types.ts
git commit -m "feat: tag detail page with works grid and top creators"
```

---

### Task 9: Frontend — Tags list page: Link-ify names

**Files:**
- Modify: `admin-web/src/app/admin/tags/page.tsx`

- [ ] **Step 1: Read current file and make tag names clickable**

Find where tag names are rendered as plain text and change to `<Link href={`/admin/tags/${tag.id}`}>`. Ensure `usage_count` is displayed.

- [ ] **Step 2: Verify build**

```bash
cd admin-web && npm run typecheck 2>&1 | tail -5
```

- [ ] **Step 3: Commit**

```bash
git add admin-web/src/app/admin/tags/page.tsx
git commit -m "feat: make tag names clickable links to tag detail"
```

---

### Task 10: Frontend — Creator detail: breadcrumb

**Files:**
- Modify: `admin-web/src/app/admin/creators/[id]/page.tsx`

- [ ] **Step 1: Add import and breadcrumb**

Add `import { Breadcrumb } from "@/components/Breadcrumb";` at top. After `<main>` opening, add:

```tsx
<Breadcrumb items={[
  { label: t("creators.title"), href: "/admin/creators" },
  { label: c.display_name || c.name },
]} />
```

- [ ] **Step 2: Ensure tag chips link to tag detail**

In tag distribution section, the tag buttons currently call `openWorksTag(tag)` which switches to works tab filtered by tag. For the chip-style buttons below the chart, change to `<Link href={`/admin/tags/${item.id}`}>` if `item.id` is available, otherwise keep the existing filter behavior.

- [ ] **Step 3: Commit**

```bash
git add admin-web/src/app/admin/creators/[id]/page.tsx
git commit -m "feat: breadcrumb on creator detail page"
```

---

### Task 11: Frontend — i18n keys

**Files:**
- Modify: `admin-web/src/lib/i18n.tsx`

- [ ] **Step 1: Add new keys**

Add to both `zh` and `en` dictionaries. New keys needed:
- `work_detail.by`, `work_detail.more_from_creator`, `work_detail.view_all_from_creator`
- `search.tab_all`, `search.tab_works`, `search.tab_creators`, `search.tab_tags`
- `search.creators_section`, `search.works_section`, `search.tags_section`
- `tag_detail.work_count`, `tag_detail.created`, `tag_detail.top_creators`
- `tag_detail.works_with_tag`, `tag_detail.no_works`
- `creators.title` (if not already present)

- [ ] **Step 2: Commit**

```bash
git add admin-web/src/lib/i18n.tsx
git commit -m "feat: i18n keys for search tabs, tag detail, breadcrumbs"
```

---

### Task 12: Deploy + Verify

- [ ] **Step 1: Build and deploy**

```bash
bash scripts/deploy.sh
```

- [ ] **Step 2: Smoke test navigation loops**

1. Works list → click creator name → Creator detail ✓
2. Work detail → breadcrumb `作品 / CreatorName / WorkTitle` ✓
3. Work detail → click creator name in header → Creator detail ✓
4. Work detail → click tag chip → Tag detail ✓
5. Tag detail → click top creator → Creator detail ✓
6. Search "ASK" → All tab shows works + creators + tags ✓
7. Search → Creators tab → click creator → Creator detail ✓
8. Works list → click SourceBadge → Works filtered by source ✓

- [ ] **Step 3: Verify build**

```bash
cd admin-web && npm run build 2>&1 | tail -10
```

Expected: Build succeeds, no type errors.

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "feat: deploy and verify UX closed-loop navigation"
```

---

## Verification Checklist

After all tasks complete:

- [ ] Works list page: creator name is blue link → `/admin/creators/{id}`
- [ ] Works list page: SourceBadge clickable → `/admin/works?source={source}`
- [ ] Work detail: breadcrumb shows `作品 / CreatorName / WorkTitle`
- [ ] Work detail: creator name in header blue link
- [ ] Work detail: "More from this creator" section with thumbnails
- [ ] Work detail: tag chips link to `/admin/tags/{id}`
- [ ] Search page: 4 tabs — All / Works / Creators / Tags
- [ ] Search page: All tab shows mixed results
- [ ] Search page: creator/tag results clickable
- [ ] Search page: breadcrumb `搜索 / "query"`
- [ ] Tag detail page: tag info + top creators + works grid
- [ ] Tag detail page: breadcrumb `标签 / #tagname`
- [ ] Creator detail page: breadcrumb `画师 / Name`
- [ ] Tags list page: tag names clickable → tag detail
- [ ] All new strings have zh + en translations
- [ ] `npm run build` passes
- [ ] `npm run typecheck` passes
