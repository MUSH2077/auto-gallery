# Code Deduplication & Refactoring — Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Eliminate duplicated library-sync logic, extract shared utilities, split admin.py monolith.

**Architecture:** Bottom-up: FileIndex singleton → image_utils → LibrarySync → admin split → callers refactor.

**Tech Stack:** Python 3.12 FastAPI + SQLAlchemy 2.0 async

## Global Constraints
- Zero API contract changes — same routes, same responses
- No new dependencies
- All existing tests must pass

---

### Task 1: FileIndex Singleton Factory
**Files:** `backend/app/services/file_index.py` + 5 caller files

Add `get_file_index()`:
```python
import os
from app.config import settings

_file_index_instance = None

def get_file_index() -> "FileIndex":
    global _file_index_instance
    if _file_index_instance is None:
        _file_index_instance = FileIndex(
            os.path.join(str(settings.download_root), ".file-index.sqlite3"))
    return _file_index_instance
```

Replace all 10 `FileIndex(os.path.join(str(settings.download_root), ".file-index.sqlite3"))` calls across `download.py`, `import_runner.py`, `import_stream.py`, `admin_data.py`, `subscription_sync.py`.

- [ ] Commit: `refactor: add FileIndex singleton factory, replace 10 call sites`

---

### Task 2: Image Utilities Extraction
**Files:** Create `backend/app/services/image_utils.py`

```python
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

def can_generate_thumbnail(suffix: str) -> bool: return suffix.lower() in IMAGE_EXTS
def get_mime_type(suffix: str) -> str | None: ...
def get_image_dims(path: str) -> tuple[int, int] | None: ...
```

Remove local definitions from `import_runner.py` and `import_stream.py`.

- [ ] Commit: `refactor: extract image utilities to shared image_utils.py`

---

### Task 3: LibrarySync Service
**Files:** Create `backend/app/services/library_sync.py`

```python
def resolve_creator_directory(source, raw_metadata, source_work_id, config) -> str: ...
async def sync_thumbnails(assets, lib_dir, file_index, source, creator_dir, work_id) -> list[str]: ...
def write_metadata_json(lib_dir, work, work_source, creator_name, assets_meta) -> None: ...
def register_metadata_in_fileindex(file_index, source, creator_dir, work_id, lib_dir) -> None: ...
```

- [ ] Commit: `refactor: add LibrarySync service`

---

### Task 4: Refactor import_runner + import_stream + admin_data
Replace inline thumbnail/metadata/fileindex with LibrarySync calls.

- [ ] Commit: `refactor: use LibrarySync in import flows and rebuild`

---

### Task 5: Refactor download.py
Replace FileIndex init with `get_file_index()`.

- [ ] Commit: `refactor: use get_file_index() in download.py`

---

### Task 6: Split admin.py (2136→6 files)
Into `backend/app/api/admin/` package:
- `__init__.py`, `settings.py`, `gallerydl.py`, `backup.py`, `data.py`, `operations.py`, `core.py`

- [ ] Commit: `refactor: split admin.py into api/admin/ package`

---

### Task 7: Verify + Deploy
```bash
docker compose run --rm backend python -m pytest -x -q
docker compose build backend
docker compose up -d --force-recreate backend worker-download worker-import worker-operations stream-import scheduler
```

- [ ] Commit: `test: verify refactoring — tests pass, services healthy`
