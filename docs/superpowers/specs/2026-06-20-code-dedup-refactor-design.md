# Code Deduplication & Refactoring — Design

**Goal:** Eliminate ~150 lines of duplicated library-sync logic across import_runner, import_stream, and admin_data; extract shared utilities; split the 2136-line admin.py monolith.

**Architecture:** Five modules — FileIndex singleton, image utilities, LibrarySync service, admin.py split, import/download slimming. Zero API contract changes.

**Tech Stack:** Python 3.12 FastAPI + SQLAlchemy 2.0 async

---

## Module 1: FileIndex Singleton Factory

`FileIndex(os.path.join(str(settings.download_root), ".file-index.sqlite3"))` appears 10 times across 5 files.

Fix: Add `get_file_index()` factory to `backend/app/services/file_index.py`. Replace all 10 call sites.

---

## Module 2: Image Utilities Extraction

`_can_generate_thumbnail()`, `_mime_type()`, `_get_image_dims()` duplicated in 3 files.

Fix: Create `backend/app/services/image_utils.py`. Move shared image functions there.

---

## Module 3: LibrarySync Service

Thumbnail generation + metadata.json write + FileIndex registration duplicated in import_runner.py, import_stream.py, and admin_data.py.

Fix: Create `backend/app/services/library_sync.py` with 4 functions:
- `resolve_creator_directory(source, raw_metadata, source_work_id, config) -> str`
- `sync_thumbnails(assets, lib_dir, file_index, ...) -> list[str]`
- `write_metadata_json(lib_dir, work, work_source, creator_name, assets) -> None`
- `register_in_fileindex(file_index, rel_path, ...) -> None`

---

## Module 4: admin.py Split (2136→~6 files)

| New File | Content | ~Lines |
|----------|---------|--------|
| `api/admin_settings.py` | Settings CRUD, reset, defaults | 200 |
| `api/admin_gallerydl.py` | gallery-dl config, cookies, auth, proxy | 300 |
| `api/admin_backup.py` | Backup create/download/restore/schedule | 150 |
| `api/admin_data.py` | Integrity, storage stats, metadata cleanup | 200 |
| `api/admin_operations.py` | Clear ops, rebuild, operation status | 100 |
| `api/admin.py` | System info, reindex, danbooru, curation | 400 |

## Module 5: Import/Download Slimming

- `import_runner.py`: 797→~500 (use LibrarySync + image_utils)
- `import_stream.py`: 355→~250 (same)
- `download.py`: 996→~850 (use get_file_index())

## Files Summary

| File | Action |
|------|--------|
| `backend/app/services/file_index.py` | Add `get_file_index()` |
| `backend/app/services/image_utils.py` | **New** |
| `backend/app/services/library_sync.py` | **New** |
| `backend/app/jobs/import_runner.py` | Refactor |
| `backend/app/jobs/import_stream.py` | Refactor |
| `backend/app/jobs/download.py` | Refactor |
| `backend/app/services/admin_data.py` | Refactor |
| `backend/app/api/admin.py` | Split |
| `backend/app/api/admin_settings.py` | **New** |
| `backend/app/api/admin_gallerydl.py` | **New** |
| `backend/app/api/admin_backup.py` | **New** |
| `backend/app/api/admin_data.py` | **New** |
| `backend/app/api/admin_operations.py` | **New** |

## Constraints

- No API contract changes
- No new dependencies
- All existing tests pass
