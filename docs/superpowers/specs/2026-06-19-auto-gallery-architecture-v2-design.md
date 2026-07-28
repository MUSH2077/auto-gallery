# auto-gallery Architecture v2 Design

**Date**: 2026-06-19
**Status**: Draft

---

## 1. Overview

auto-gallery is a NAS-hosted multi-source media archive and gallery manager. It downloads from 8 platforms via gallery-dl, parses metadata JSON, and imports into PostgreSQL + Meilisearch.

Four priority areas:
1. **Proxy compatibility** — gallery-dl hangs behind Clash/V2Ray proxy
2. **Large-scale sync** — 5000+ work accounts need hours of incremental sync
3. **File scanning** — NAS HDD rglob scans are slow
4. **Frontend UX** — distinguish auto-recovering vs manual-action-needed

---

## 2. Architecture: Event-Driven Streaming Pipeline

### v1 (Current): Serial Two-Phase
```
Download Worker → gallery-dl → ALL files → ALL import → complete
```

### v2 (Target): Event-Driven Streaming
```
Scheduler → RQ "downloads:{source}" → Download Worker
  → gallery-dl subprocess
  → per-work completion → Redis Stream "work:ready"
  → Import Worker(s) consume via consumer group
  → parse → DB → thumbnail → File Index mark done
```

Key changes:
- Download and import are **decoupled** — import starts as first work finishes
- File Index (SQLite) replaces rglob filesystem scans
- Redis Stream consumer groups enable parallel import with dedup

---

## 3. Directory Alignment

Both storage roots use the **same** gallery-dl directory template:

```
/downloads/pixiv/{user[account]}/{work_id}/file.jpg
/library/pixiv/{user[account]}/{work_id}/metadata.json
/library/pixiv/{user[account]}/{work_id}/thumbnail.webp
```

import_runner constructs library path from the effective gallery-dl config template, substituting variables from work metadata JSON.

---

## 4. File Index (SQLite)

Located at `{DOWNLOAD_ROOT}/.file-index.sqlite3`. Replaces rglob scans.

### Schema
```sql
CREATE TABLE file_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT UNIQUE NOT NULL,
    storage_root TEXT NOT NULL,         -- 'downloads' | 'library'
    source TEXT NOT NULL,
    creator_dir TEXT NOT NULL,
    work_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_type TEXT NOT NULL,            -- 'metadata_json' | 'image' | 'thumbnail' | 'archive'
    file_size INTEGER,
    download_job_id TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    import_status TEXT DEFAULT 'new',   -- 'new' | 'importing' | 'done' | 'skipped'
    import_job_id TEXT
);
CREATE INDEX idx_file_index_work ON file_index(source, creator_dir, work_id);
CREATE INDEX idx_file_index_status ON file_index(import_status, first_seen_at);
CREATE INDEX idx_file_index_type ON file_index(file_type, import_status);
```

### Operations
| Operation | v1 | v2 |
|-----------|----|----|
| Find new JSONs | rglob O(n) | SELECT WHERE status='new' O(log n) |
| Count artifacts | rglob + set diff | SELECT COUNT WHERE job_id=? |
| Mark imported | delete JSON file | UPDATE status='done' |

---

## 5. Redis Stream: "work:ready"

Replaces `import:{job_id}:files` Redis key pattern.

### Stream Design
```
Stream: work:ready
Consumer Group: import-workers
Message: {download_job_id, source, work_id, json_path, asset_paths, creator_dir}
```

### Producer (download.py)
When gallery-dl writes a metadata JSON:
1. Detect via file mtime polling in stderr reader thread
2. Read JSON, extract work_id + asset paths
3. XADD work:ready
4. Register in File Index

### Consumer (new import_stream.py)
```
XREADGROUP GROUP import-workers BLOCK 5000 STREAMS work:ready >
  → check File Index idempotency
  → parse → DB → thumbnail → library metadata.json
  → UPDATE file_index SET status='done'
  → XACK
```

Benefits: parallel consumers, exactly-once via XACK + File Index, survives restarts.

---

## 6. Worker Pool: Per-Source Queues

### v1
```
Queue "downloads" → all sources
Queue "imports" → all imports
```

### v2
```
Queue "downloads:pixiv" / "downloads:danbooru" / ... (one per source)
Stream "work:ready" → consumer group → N parallel import workers
```

### docker-compose changes
```yaml
worker-download:
  command: python worker_entrypoint.py downloads:pixiv,downloads:danbooru,... 3

worker-import:
  command: python worker_entrypoint.py --stream work:ready --group import-workers 2
```

---

## 7. Proxy Health Management

- Pre-flight runs before each download (DNS + TCP check)
- 3 consecutive failures → scheduler skips source with status "down"
- Recovery probe every 30 min: single HEAD request → success resets
- Frontend: Proxy settings page shows 24h connectivity history

---

## 8. Frontend (v2 additions)

| Feature | Description |
|---------|-------------|
| Real-time work counter | WebSocket push per imported work on Dashboard |
| ETA display | Download rate → estimated completion |
| Proxy health chart | 24h success rate on Proxy settings page |
| Directory template preview | gallery-dl settings shows live path preview |
| Per-source dashboard | Rate, queue depth, success rate per source |

Already done in v1.5: status classification, error classification, border colors, retry progress, download defaults, scheduler batch, clear library.

---

## 9. Implementation Phases

### Phase 1: Foundation
- Proxy fix: docker-compose network access to mihomo
- File Index module (`services/file_index.py`)
- Library directory alignment (`import_runner.py`)
- Replace rglob with File Index queries (`download.py`)

### Phase 2: Streaming Pipeline
- Redis Stream producer in download worker
- New `jobs/import_stream.py` consumer
- Remove old import job creation flow
- Consumer group config in `worker_entrypoint.py`

### Phase 3: Parallelism + Frontend
- Per-source RQ queues
- Multi-queue worker entrypoint
- Proxy health table + recovery logic
- Frontend dashboard refresh, proxy chart, directory preview

---

## 10. Migration

- **Existing files**: File Index bootstraps by scanning trees once, then incremental
- **Backward compat**: Old import_jobs kept; new flow creates records with `stream_mode=true`
- **Rollback**: File Index additive; old rglob path remains as fallback
- **Thumbnails**: Existing detected by File Index bootstrap, no regeneration needed
