# Phase 1: Foundation — Proxy Fix + File Index + Directory Alignment

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix proxy connectivity for gallery-dl, add SQLite file index to replace rglob scans, align library directory structure with downloads.

**Architecture:** Mihomo proxy runs in `network_mode: host` on NAS. auto-gallery containers need `extra_hosts` to reach it. File Index is a lightweight SQLite DB at `/downloads/.file-index.sqlite3` that tracks every file with O(log n) queries. Library directories use the same gallery-dl config template as downloads.

**Tech Stack:** Python 3.12, SQLite3 (stdlib), gallery-dl, Docker Compose v2

## Global Constraints

- Sqlite3 from Python stdlib — no additional dependencies
- File Index is additive — old rglob path remains as fallback
- Library directory alignment is backward compatible — existing files not moved
- Proxy fix is docker-compose config only — no code changes
- `shell=True` FORBIDDEN
- Commit after each task

---

### Task 1: Proxy Fix — Docker Compose Network Access

**Files:**
- Modify: `docker-compose.yaml:99-160` (worker-download, worker-import, scheduler services)

**Interfaces:**
- Consumes: Mihomo in `network_mode: host` on NAS at `192.168.10.170:7890`
- Produces: Containers can reach `host.docker.internal` → NAS host → mihomo proxy

- [ ] **Step 1: Add extra_hosts to worker-download, worker-import, and scheduler**

After each service's `volumes:` block, add:

```yaml
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

Apply to these three services: `worker-download`, `worker-import`, `scheduler`.

- [ ] **Step 2: Restart affected containers**

```bash
docker compose up -d --force-recreate worker-download worker-import scheduler
```

- [ ] **Step 3: Verify proxy connectivity**

```bash
docker exec auto-gallery-worker-download-1 sh -c "ping -c1 host.docker.internal 2>&1 || echo 'ping failed but DNS may still work'"
```

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yaml
git commit -m "fix: add extra_hosts for proxy access to mihomo on NAS host"
```

---

### Task 2: File Index Module

**Files:**
- Create: `backend/app/services/file_index.py`

**Interfaces:**
- Produces:
  - `FileIndex(db_path: str)` — constructor
  - `FileIndex.upsert(file_path, storage_root, source, creator_dir, work_id, file_name, file_type, file_size, download_job_id) -> None`
  - `FileIndex.mark_imported(file_path, import_job_id) -> None`
  - `FileIndex.mark_importing(file_path, import_job_id) -> None`
  - `FileIndex.get_new_metadata_jsons(source, download_job_id) -> list[str]`
  - `FileIndex.count_new_artifacts(source, download_job_id) -> tuple[int, int, list[str]]`
  - `FileIndex.bootstrap_if_empty(download_root, library_root) -> int`
  - `FileIndex.vacuum() -> None`

- [ ] **Step 1: Create the file**

```python
"""SQLite file index — replaces rglob filesystem scans with O(log n) queries.

Located at {DOWNLOAD_ROOT}/.file-index.sqlite3. Tracks every file in
/downloads/ and /library/ with source, creator_dir, work_id, and import status.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS file_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT UNIQUE NOT NULL,
    storage_root TEXT NOT NULL,
    source TEXT NOT NULL,
    creator_dir TEXT NOT NULL,
    work_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_type TEXT NOT NULL,
    file_size INTEGER,
    download_job_id TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    import_status TEXT NOT NULL DEFAULT 'new',
    import_job_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_file_index_work
    ON file_index(source, creator_dir, work_id);
CREATE INDEX IF NOT EXISTS idx_file_index_status
    ON file_index(import_status, first_seen_at);
CREATE INDEX IF NOT EXISTS idx_file_index_type
    ON file_index(file_type, import_status);
CREATE INDEX IF NOT EXISTS idx_file_index_job
    ON file_index(download_job_id, import_status);
"""

IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".zip"}


def _classify_file_type(file_name: str) -> str:
    lower = file_name.lower()
    if lower.endswith(".json"):
        return "metadata_json"
    ext = os.path.splitext(lower)[1]
    if ext in IMG_EXTS:
        return "image"
    if ext == ".zip":
        return "archive"
    if lower.endswith(".webp"):
        return "thumbnail"
    return "unknown"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class FileIndex:
    """Thread-safe SQLite file index for downloads and library storage."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.executescript(SCHEMA_SQL)
                conn.commit()
            finally:
                conn.close()

    def upsert(self, file_path, storage_root, source, creator_dir,
               work_id, file_name, file_type="", file_size=0,
               download_job_id="", import_status="new") -> None:
        now = _now_iso()
        if not file_type:
            file_type = _classify_file_type(file_name)
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    """INSERT INTO file_index
                       (file_path, storage_root, source, creator_dir, work_id,
                        file_name, file_type, file_size, download_job_id,
                        first_seen_at, last_seen_at, import_status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(file_path) DO UPDATE SET
                        last_seen_at = excluded.last_seen_at,
                        file_size = COALESCE(excluded.file_size, file_index.file_size),
                        download_job_id = COALESCE(excluded.download_job_id, file_index.download_job_id)""",
                    (file_path, storage_root, source, creator_dir, work_id,
                     file_name, file_type, file_size or 0, download_job_id or "",
                     now, now, import_status),
                )
                conn.commit()
            finally:
                conn.close()

    def mark_imported(self, file_path, import_job_id="") -> None:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    "UPDATE file_index SET import_status='done', import_job_id=?, last_seen_at=? WHERE file_path=?",
                    (import_job_id, _now_iso(), file_path),
                )
                conn.commit()
            finally:
                conn.close()

    def mark_importing(self, file_path, import_job_id) -> None:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    "UPDATE file_index SET import_status='importing', import_job_id=?, last_seen_at=? WHERE file_path=? AND import_status='new'",
                    (import_job_id, _now_iso(), file_path),
                )
                conn.commit()
            finally:
                conn.close()

    def get_new_metadata_jsons(self, source, download_job_id="") -> list[str]:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                cur = conn.execute(
                    """SELECT file_path FROM file_index
                       WHERE file_type='metadata_json' AND import_status='new'
                       AND source=? AND (?='' OR download_job_id=?)
                       ORDER BY first_seen_at""",
                    (source, download_job_id, download_job_id),
                )
                return [row[0] for row in cur.fetchall()]
            finally:
                conn.close()

    def count_new_artifacts(self, source, download_job_id) -> tuple[int, int, list[str]]:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                cur = conn.execute(
                    "SELECT file_path, file_type FROM file_index WHERE download_job_id=? AND import_status='new'",
                    (download_job_id,),
                )
                rows = cur.fetchall()
                json_count = sum(1 for _, ft in rows if ft == "metadata_json")
                img_count = sum(1 for _, ft in rows if ft == "image")
                json_paths = [p for p, ft in rows if ft == "metadata_json"]
                return json_count, img_count, json_paths
            finally:
                conn.close()

    def bootstrap_if_empty(self, download_root, library_root) -> int:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                count = conn.execute("SELECT COUNT(*) FROM file_index").fetchone()[0]
            finally:
                conn.close()
        if count > 0:
            return 0
        logger.info("Bootstrapping file index from %s and %s ...", download_root, library_root)
        total = 0
        for root_label, root_path in [("downloads", download_root), ("library", library_root)]:
            root = Path(root_path)
            if not root.exists():
                continue
            for fpath in root.rglob("*"):
                if not fpath.is_file():
                    continue
                rel = str(fpath.relative_to(root))
                parts = rel.split("/")
                if len(parts) < 3:
                    continue
                source = parts[0]
                creator_dir = parts[1]
                work_id = parts[2]
                import_status = "done" if root_label == "library" else "new"
                self.upsert(
                    file_path=rel, storage_root=root_label,
                    source=source, creator_dir=creator_dir,
                    work_id=work_id, file_name=fpath.name,
                    file_type=_classify_file_type(fpath.name),
                    file_size=fpath.stat().st_size,
                    import_status=import_status,
                )
                total += 1
                if total % 1000 == 0:
                    logger.info("Bootstrapped %d files ...", total)
        logger.info("File index bootstrap complete: %d files indexed", total)
        return total

    def vacuum(self) -> None:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute("VACUUM")
            finally:
                conn.close()
```

- [ ] **Step 2: Verify syntax**

```bash
python3 -c "import ast; ast.parse(open('backend/app/services/file_index.py').read()); print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/file_index.py
git commit -m "feat: add FileIndex SQLite module to replace rglob scans"
```

---

### Task 3: Initialize FileIndex on Worker Startup + Register Files

**Files:**
- Modify: `backend/app/jobs/download.py:1-27` (add import)
- Modify: `backend/app/jobs/download.py:257-265` (after dl_defaults read)
- Modify: `backend/app/jobs/download.py:608` (after manifest recording, register new files)
- Modify: `backend/app/jobs/download.py:650-656` (replace _count_new_artifacts)
- Modify: `backend/app/jobs/download.py:723` (replace _count_new_artifacts in error path)
- Modify: `backend/app/jobs/download.py:732` (replace _count_new_artifacts in timeout path)

**Interfaces:**
- Consumes: `FileIndex` from `app.services.file_index`
- Produces: File index initialized; rglob replaced with FileIndex queries

- [ ] **Step 1: Add import in download.py**

After `from app.services.settings import ...`:
```python
from app.services.file_index import FileIndex
```

- [ ] **Step 2: Initialize FileIndex after reading defaults**

After `dl_timeout = int(dl_defaults.get(...))` block (~line 263):
```python
# Initialize file index (one-time bootstrap on first run)
_file_index = FileIndex(os.path.join(str(settings.download_root), ".file-index.sqlite3"))
```

- [ ] **Step 3: Add file registration function after manifest recording**

After `await _manifest_db.commit()` (~line 608), add:
```python
# Register newly downloaded files in FileIndex
def _register_new_files():
    source_root = Path(settings.download_root) / job.source
    if not source_root.exists():
        return 0
    count = 0
    IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    for jf in source_root.rglob("*.json"):
        if not jf.is_file():
            continue
        rel = str(jf.relative_to(settings.download_root))
        parts = rel.split("/")
        if len(parts) < 3:
            continue
        _file_index.upsert(
            file_path=rel, storage_root="downloads", source=job.source,
            creator_dir=parts[1], work_id=parts[2], file_name=jf.name,
            file_type="metadata_json", file_size=jf.stat().st_size,
            download_job_id=str(job_id),
        )
        count += 1
        work_dir = jf.parent
        for af in work_dir.iterdir():
            if af.is_file() and af.suffix.lower() in IMG_EXTS:
                arel = str(af.relative_to(settings.download_root))
                _file_index.upsert(
                    file_path=arel, storage_root="downloads", source=job.source,
                    creator_dir=parts[1], work_id=parts[2], file_name=af.name,
                    file_type="image", file_size=af.stat().st_size,
                    download_job_id=str(job_id),
                )
    return count

_registered = _register_new_files()
logger.info("Registered %d new files in FileIndex for job %s", _registered, job_id)
```

- [ ] **Step 4: Replace _count_new_artifacts calls**

At all three call sites (normal path ~line 654, error path ~line 723, timeout path ~line 732), replace:
```python
# OLD:
metadata_count, image_count, new_json_paths = _count_new_artifacts(job.source, json_before)

# NEW:
_file_index = FileIndex(os.path.join(str(settings.download_root), ".file-index.sqlite3"))
metadata_count, image_count, new_json_paths = _file_index.count_new_artifacts(job.source, str(job_id))
```

- [ ] **Step 5: Remove the json_before snapshot**

Delete the line `json_before: set[str] = _snapshot_metadata_jsons(job.source)` (~line 328). Not needed anymore — FileIndex tracks state.

- [ ] **Step 6: Verify syntax**

```bash
python3 -c "import ast; ast.parse(open('backend/app/jobs/download.py').read()); print('OK')"
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/jobs/download.py
git commit -m "feat: integrate FileIndex into download pipeline, replace rglob"
```

---

### Task 4: Library Directory Alignment

**Files:**
- Modify: `backend/app/jobs/import_runner.py:480-484`

**Interfaces:**
- Consumes: gallery-dl config from `services/settings.py`
- Produces: Library path matches downloads path (same template)

- [ ] **Step 1: Replace lib_dir construction**

Old (~line 480):
```python
lib_dir = (Path(settings.library_root) / provider.source_name
           / dir_name / src_work_id)
```

New:
```python
# Use gallery-dl directory template for library path (aligns with downloads)
from app.services.settings import load_gallerydl_config, extractor_key_for_source
_extractor_key = extractor_key_for_source(provider.source_name)
_config = load_gallerydl_config()
_extractor_cfg = _config.get("extractor", {}).get(_extractor_key, {})
_dir_template = _extractor_cfg.get("directory", [provider.source_name, "{id}"])

_resolved_parts = []
for part in _dir_template:
    resolved = part
    if isinstance(first_raw, dict):
        for k, v in first_raw.items():
            if isinstance(v, dict):
                for sk, sv in v.items():
                    resolved = resolved.replace(f"{{user[{sk}]}}", str(sv) if sv else "")
                    resolved = resolved.replace(f"{{{sk}}}", str(sv) if sv else "")
            elif isinstance(v, (str, int, float)):
                resolved = resolved.replace(f"{{{k}}}", str(v) if v is not None else "")
    resolved = resolved.replace("{id}", src_work_id)
    resolved = resolved.replace("{category}", str(first_raw.get("category", "")))
    resolved = resolved.replace("{board[name]}", str(first_raw.get("board", {}).get("name", "") if isinstance(first_raw.get("board"), dict) else ""))
    _resolved_parts.append(resolved.strip().replace("/", "_"))

_creator_dir = _resolved_parts[1] if len(_resolved_parts) > 1 else dir_name
lib_dir = (Path(settings.library_root) / provider.source_name
           / _creator_dir / src_work_id)
```

- [ ] **Step 2: Verify syntax**

```bash
python3 -c "import ast; ast.parse(open('backend/app/jobs/import_runner.py').read()); print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add backend/app/jobs/import_runner.py
git commit -m "feat: align library directory with gallery-dl download template"
```

---

### Task 5: Use FileIndex in import_runner for file discovery

**Files:**
- Modify: `backend/app/jobs/import_runner.py:235-291` (file discovery section)

- [ ] **Step 1: Add FileIndex-based file discovery**

Replace the current file discovery with:
```python
# Get new JSONs from FileIndex (primary), fall back to Redis key (backward compat)
from app.services.file_index import FileIndex
_file_index = FileIndex(os.path.join(str(settings.download_root), ".file-index.sqlite3"))
json_rel_paths = _file_index.get_new_metadata_jsons(provider.source_name, str(dj.id))

all_json_files = sorted(
    Path(settings.download_root) / p for p in json_rel_paths
    if (Path(settings.download_root) / p).exists()
)

if not all_json_files:
    from app.services.redis_client import get_redis
    legacy = _consume_import_file_list(get_redis(), import_job_id)
    if legacy:
        all_json_files = sorted(Path(p) for p in legacy if Path(p).exists())
```

- [ ] **Step 2: Mark importing/done during processing**

Before parsing (~line 363):
```python
_json_rel = str(jf.relative_to(settings.download_root))
_file_index.mark_importing(_json_rel, import_job_id)
```

After deleting processed JSON (~line 641):
```python
_file_index.mark_imported(_json_rel, import_job_id)
```

- [ ] **Step 3: Commit**

```bash
git add backend/app/jobs/import_runner.py
git commit -m "feat: use FileIndex for import file discovery with legacy fallback"
```

---

### Task 6: VACUUM FileIndex in Scheduler

**Files:**
- Modify: `backend/app/jobs/subscription_sync.py:358-370`

- [ ] **Step 1: Add FileIndex VACUUM**

In the archive VACUUM loop, add:
```python
try:
    from app.services.file_index import FileIndex
    fi_path = os.path.join(str(settings.download_root), ".file-index.sqlite3")
    if os.path.exists(fi_path):
        FileIndex(fi_path).vacuum()
        logger.debug("VACUUMed file index")
except Exception:
    logger.debug("File index VACUUM skipped", exc_info=True)
```

- [ ] **Step 2: Commit**

```bash
git add backend/app/jobs/subscription_sync.py
git commit -m "feat: add periodic VACUUM of FileIndex in scheduler"
```

---

### Task 7: Build and Smoke Test

- [ ] **Step 1: Rebuild and restart all containers**

```bash
cd /volume3/docker/auto-gallery
docker compose build backend
docker compose up -d --force-recreate backend worker-download worker-import scheduler admin-web
```

- [ ] **Step 2: Verify containers healthy**

```bash
for c in backend worker-download worker-import scheduler admin-web; do
  docker inspect auto-gallery-$c-1 --format '{{.Name}} {{.State.Health.Status}}'
done
```

- [ ] **Step 3: Verify FileIndex bootstrap**

```bash
docker exec auto-gallery-worker-download-1 python3 -c "
from app.services.file_index import FileIndex
fi = FileIndex('/downloads/.file-index.sqlite3')
import sqlite3
c = sqlite3.connect('/downloads/.file-index.sqlite3')
print('Total files:', c.execute('SELECT COUNT(*) FROM file_index').fetchone()[0])
print('By source:', c.execute('SELECT source, COUNT(*) FROM file_index GROUP BY source').fetchall())
c.close()
"
```

- [ ] **Step 4: Trigger a sync and observe**

Trigger a subscription sync from the admin UI. Verify:
- File index is populated with new entries
- Import processes work through FileIndex (no "scanning metadata files" rglob message)
- Library directory structure matches downloads

- [ ] **Step 5: Commit**

```bash
git add .
git commit -m "chore: Phase 1 complete — proxy fix + FileIndex + directory alignment"
```
