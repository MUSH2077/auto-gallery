# Filesystem Path Constraints

## Core Rule

**NEVER assume a fixed directory structure under `download_root` or `library_root`.**

The file tree under `/downloads/{source}/` is determined by user-configurable naming templates set per source at `/admin/settings/gallerydl`. Each source provider (pixiv, danbooru, iwara, etc.) can have a different directory pattern and filename pattern.

## Naming Template System

Naming templates are stored in the `naming_templates` table:

| Column | Type | Purpose |
|--------|------|---------|
| `name` | string | Human-readable label |
| `source` | string/null | Source provider name (null = global default) |
| `template` | string | gallery-dl directory template (JSON array string) |
| `is_default` | bool | Active template for this source |

The `template` field is passed directly to gallery-dl as the `directory` config for the extractor. Example values:

```
["pixiv", "{user[account]}", "{id}"]
["danbooru", "{category}", "{id}"]
```

The user can change these at any time via the admin web UI.

## How gallery-dl Uses Templates

In `download.py`, `build_gallerydl_config()` writes a per-job config to `{GALLERYDL_CONFIG_ROOT}/jobs/job-{id}.json` containing the naming template's `directory` setting. gallery-dl then outputs files to:

```
{download_root}/{directory_template_parts}/{image_files}
```

The resulting structure is entirely determined by the naming template. There is no fixed pattern.

## Rules for Code That Touches the Filesystem

### 1. DO NOT hardcode directory paths

BAD:
```python
creator_dir = download_root / source / creator_id
work_dir = creator_dir / work_id
```

GOOD — read actual filesystem:
```python
source_root = Path(settings.download_root) / source_name
for json_file in source_root.rglob("*.json"):
    # Find metadata JSONs wherever they are
```

### 2. DO use before/after snapshots to detect new files

When you need to know if an operation produced new files, snapshot the relevant state BEFORE the operation and diff AFTER:

```python
json_before = {str(p) for p in source_root.rglob("*.json") if p.is_file()}
# ... run operation ...
json_after = {str(p) for p in source_root.rglob("*.json") if p.is_file()}
new_files = json_after - json_before
```

This works regardless of naming template configuration.

### 3. `download_dir` is a HINT, not a contract

`DownloadJob.download_dir` is populated by `provider.get_creator_dir_from_url()` before gallery-dl runs. It is the provider's best guess at where files will land, but the actual output directory depends on the naming template. Always fall back to scanning the source root when the hint does not match.

### 4. Library directory structure is separate

The library (`/library/`) stores per-work metadata + thumbnails. Its structure is currently `{source}/{creator_dir}/{work_id}/` where `creator_dir` comes from `provider.get_creator_directory_name()`. This is independent of the download naming template.

### 5. Storage breakdown walks actual filesystem

`storage_breakdown` and `storage_stats` endpoints walk the ACTUAL filesystem to measure disk usage. They do not make assumptions about directory structure — they simply iterate whatever directories exist.

## Files That Must Follow These Rules

| File | Concern |
|------|---------|
| `backend/app/jobs/download.py` | `_snapshot_metadata_jsons()` / `_count_new_artifacts()` — uses before/after diff |
| `backend/app/jobs/import_runner.py` | `run_import_job()` — scans `source_root.rglob("*.json")`, uses `download_dir` as hint with fallback |
| `backend/app/api/admin.py` | `storage_breakdown()` — walks actual filesystem structure |
| `backend/app/api/system.py` | `storage_stats()` — walks actual filesystem structure |
| `backend/app/api/media.py` | Serves files using `asset.file_path` recorded at import time |

## When Adding New Features

Before implementing any feature that reads or writes files under `download_root` or `library_root`:

1. Check if a naming template could change the directory structure
2. Use `rglob()` / `iterdir()` to discover files rather than constructing paths
3. Use snapshot diffing to attribute file changes to a specific operation
4. Never assume `{source}/{creator_id}/{work_id}` is the actual layout
