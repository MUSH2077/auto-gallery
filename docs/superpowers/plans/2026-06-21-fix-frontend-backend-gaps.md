# Fix Frontend-Backend Integration Gaps

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 6 integration gaps found in the frontend-backend audit.

**Architecture:** Targeted fixes to frontend API client, query keys, i18n, and Docker state. No new backend routes needed — dead frontend code is removed instead.

**Tech Stack:** TypeScript, Next.js 14, TanStack Query, i18n, Docker Compose

## Global Constraints

- Don't break any existing functionality
- No new backend routes unless essential
- All query keys must go through `queryKeys` factory
- Dead code must be removed, not commented out

---

### Task 1: Remove dead `syncDanbooruFavorites` API client code

**Files:**
- Modify: `admin-web/src/lib/api/index.ts`

**Why:** Frontend defines `syncDanbooruFavorites` calling `POST /api/v1/reference/danbooru/favorites/sync` but no backend route exists and no page calls this function. Remove the dead code.

- [ ] **Step 1: Remove the function from the api object**

Remove lines 465-471 from `admin-web/src/lib/api/index.ts`:

```typescript
  syncDanbooruFavorites: () =>
    request<{
      status: string; message?: string;
      total_favorites?: number; created: number; matched: number; errors: number;
      details?: { artist_name: string; danbooru_id?: number; action: string;
                  creator_id?: string; post_count?: number; error?: string }[];
    }>("/api/v1/reference/danbooru/favorites/sync", { method: "POST" }),
```

- [ ] **Step 2: Verify no other references**

```bash
grep -rn "syncDanbooruFavorites" admin-web/src/ --include="*.tsx" --include="*.ts"
```
Expected: Only matches in the `api/index.ts` definition itself (already removed).

---

### Task 2: Clean orphan Docker container

**Files:** None (infrastructure only)

- [ ] **Step 1: Remove orphan containers**

```bash
cd /volume2/docker/auto-gallery && docker compose down --remove-orphans
```

- [ ] **Step 2: Restart services**

```bash
docker compose up -d
```

- [ ] **Step 3: Verify no orphan warnings**

```bash
docker compose ps
```
Expected: No "Found orphan containers" warnings.

---

### Task 3: Complete the queryKeys factory

**Files:**
- Modify: `admin-web/src/lib/api/index.ts` (queryKeys section)

**Why:** ~20 API calls use hardcoded query key strings instead of the `queryKeys` factory, making cache invalidation unreliable.

- [ ] **Step 1: Add missing entries to queryKeys factory**

Replace the `queryKeys` object (lines ~533-585) with:

```typescript
export const queryKeys = {
  health: ["health"] as const,
  workbench: ["system", "workbench"] as const,
  schedulerDecisions: ["system", "scheduler-decisions"] as const,
  sources: ["sources"] as const,
  creators: {
    all: ["creators"] as const,
    count: ["creators", "count"] as const,
    list: (page = 0, limit = 50, filters?: unknown) => ["creators", "list", page, limit, filters || {}] as const,
    detail: (id: string) => ["creators", id] as const,
    links: (id: string) => ["creators", id, "links"] as const,
    duplicates: ["creators", "duplicates"] as const,
  },
  subscriptions: {
    all: ["subscriptions"] as const,
    count: ["subscriptions", "count"] as const,
    list: (page = 0, limit = 50, filters?: unknown) => ["subscriptions", "list", page, limit, filters || {}] as const,
    detail: (id: string) => ["subscriptions", id] as const,
    sources: (id: string) => ["subscriptions", id, "sources"] as const,
  },
  repositories: {
    detail: (id: string) => ["repositories", id] as const,
    graph: (id: string, offset = 0, params?: unknown) => ["repositories", id, "curation-graph", offset, params || {}] as const,
  },
  curation: {
    all: ["curation"] as const,
    commits: (params?: unknown) => ["curation", "commits", params || {}] as const,
    subject: (type: string, id: string) => ["curation", "subject", type, id] as const,
    suggestions: ["curation", "rule-suggestions"] as const,
    backfillStatus: ["curation", "backfill", "status"] as const,
  },
  downloadJobs: {
    all: ["download-jobs"] as const,
    detail: (id: string) => ["download-jobs", id] as const,
    imports: (id: string) => ["download-jobs", id, "imports"] as const,
    progress: (id: string) => ["download-jobs", id, "progress"] as const,
    pipeline: (id: string) => ["download-jobs", id, "pipeline"] as const,
  },
  importJobs: {
    all: ["import-jobs"] as const,
    detail: (id: string) => ["import-jobs", id] as const,
  },
  works: {
    all: ["works"] as const,
    detail: (id: string) => ["works", id] as const,
    sources: (id: string) => ["works", id, "sources"] as const,
    assets: (id: string) => ["works", id, "assets"] as const,
    tags: (id: string) => ["works", id, "tags"] as const,
  },
  tags: {
    all: ["tags"] as const,
    detail: (id: string) => ["tags", id] as const,
  },
  admin: {
    settings: ["admin", "settings"] as const,
    gallerydlConfig: ["admin", "gallerydl-config"] as const,
    authStatus: ["admin", "auth-status"] as const,
  },
  reference: {
    danbooru: ["reference", "danbooru"] as const,
  },
  system: {
    logs: (limit?: number, level?: string, name?: string) => ["system", "logs", limit, level, name] as const,
    storage: ["system", "storage"] as const,
    queueStats: ["system", "queue-stats"] as const,
    systemInfo: ["system", "info"] as const,
    integrityCheck: ["system", "integrity-check"] as const,
  },
  backups: {
    list: ["backups", "list"] as const,
    estimate: ["backups", "estimate"] as const,
  },
  dedup: {
    duplicates: ["dedup", "duplicates"] as const,
    mergeCandidates: ["dedup", "merge-candidates"] as const,
  },
} as const;
```

---

### Task 4: Replace hardcoded query keys in pages

**Files:**
- Modify: `admin-web/src/app/admin/dedup/page.tsx`
- Modify: `admin-web/src/app/admin/merge-candidates/page.tsx`
- Modify: `admin-web/src/app/admin/settings/auth-status/page.tsx`
- Modify: `admin-web/src/app/admin/creators/duplicates/page.tsx`
- Modify: `admin-web/src/app/admin/settings/backup/page.tsx`

- [ ] **Step 1: Fix `dedup/page.tsx`**

Line 12: Replace `queryKey: ["dedup-duplicates"]` with `queryKey: queryKeys.dedup.duplicates`

```typescript
const dups = useQuery({ queryKey: queryKeys.dedup.duplicates, queryFn: api.listDuplicates });
```

Also fix the invalidation on line 13:
```typescript
const scan = useMutation({ mutationFn: api.scanDuplicates, onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.dedup.duplicates }) });
```

- [ ] **Step 2: Fix `merge-candidates/page.tsx`**

Line 11: Replace `queryKey: ["merge-candidates"]` with `queryKey: queryKeys.dedup.mergeCandidates`

```typescript
const mc = useQuery({ queryKey: queryKeys.dedup.mergeCandidates, queryFn: api.listMergeCandidates });
```

- [ ] **Step 3: Fix `settings/auth-status/page.tsx`**

Line 10: Replace `queryKey: ["auth-status"]` with `queryKey: queryKeys.admin.authStatus`

```typescript
const auth = useQuery({ queryKey: queryKeys.admin.authStatus, queryFn: api.getAuthStatus, refetchInterval: 30000 });
```

- [ ] **Step 4: Fix `creators/duplicates/page.tsx`**

Line 13: Replace `queryKey: ["creator-duplicates"]` with `queryKey: queryKeys.creators.duplicates`

```typescript
const dups = useQuery({ queryKey: queryKeys.creators.duplicates, queryFn: api.listDuplicateCreators });
```

- [ ] **Step 5: Fix `settings/backup/page.tsx`**

Line 51-52: Replace hardcoded keys

```typescript
const backups = useQuery({ queryKey: queryKeys.backups.list, queryFn: api.listBackups });
const estimate = useQuery({ queryKey: queryKeys.backups.estimate, queryFn: () => api.estimateBackupSizes() });
```

- [ ] **Step 6: Verify no remaining hardcoded query key strings**

```bash
grep -rn 'queryKey: \["' admin-web/src/app/admin/ --include="*.tsx"
```
Expected: No matches (all pages use queryKeys factory).

---

### Task 5: Remove dead i18n keys

**Files:**
- Modify: `admin-web/src/lib/i18n.tsx`

- [ ] **Step 1: Remove zh-CN dead keys**

Remove these lines from the `zh` object:
```
"dashboard.original_media_size": "{size} 原图仓库",
"dashboard.library": "索引库",  
"dashboard.index_files": "{count} 个索引文件",
"dashboard.library_files": "库 · {count} 个文件",
```

- [ ] **Step 2: Remove en dead keys**

Remove these lines from the `en` object:
```
"dashboard.original_media_size": "{size} original media",
"dashboard.library": "Library",
"dashboard.index_files": "{count} index files",
"dashboard.library_files": "Library · {count} files",
```

- [ ] **Step 3: Verify keys are no longer referenced in code**

```bash
grep -rn "dashboard.original_media_size\|dashboard.index_files\|dashboard.library\b\|dashboard.library_files" admin-web/src/ --include="*.tsx"
```
Expected: No matches.

---

### Task 6: Rebuild and deploy

**Files:** None (infrastructure only)

- [ ] **Step 1: Rebuild admin-web image**

```bash
cd /volume2/docker/auto-gallery && docker compose build admin-web
```

- [ ] **Step 2: Recreate admin-web container**

```bash
docker compose up -d --force-recreate admin-web
```

- [ ] **Step 3: Smoke test**

```bash
curl -sf http://localhost:13000/ | head -5
```
Expected: HTML output (Next.js page renders).
