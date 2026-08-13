# Library Index Rebuild — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** Add "Rebuild Library Index" that regenerates /library/ metadata.json + thumbnails from the database.

**Architecture:** Async function traverses Work→WorkSource→Asset→AssetSource chain, reconstructs gallery-dl directory paths, writes metadata.json + thumbnails, updates FileIndex.

**Tech Stack:** Python 3.12, SQLAlchemy async, pyvips, Redis progress tracking

## Global Constraints

- `shell=True` FORBIDDEN
- Reuse `generate_thumbnail()` from `app.services.thumbnail`
- Reuse gallery-dl directory template logic from `import_runner.py`
- Commit after each task

---

### Task 1: Backend — Library Rebuild Service + API Endpoint

**Files:**
- Modify: `backend/app/services/admin_data.py` — add `rebuild_library_index()`
- Modify: `backend/app/api/admin.py` — add `POST /admin/library/rebuild`

**Interfaces:**
- Produces: `rebuild_library_index(db: AsyncSession) -> dict` returning `{status, message, rebuilt, errors}`

- [ ] **Step 1: Add rebuild_library_index to admin_data.py**

Insert after the `clear_entity_data` function:

```python
async def rebuild_library_index(db: AsyncSession) -> dict:
    """Regenerate all /library/ metadata.json + thumbnails from DB records."""
    from pathlib import Path
    import json, os, asyncio
    from sqlalchemy import select
    from app.config import settings
    from app.models import Work, Asset, WorkSource, AssetSource, SourceCreator
    from app.services.file_index import FileIndex
    from app.services.thumbnail import generate_thumbnail
    from app.services.settings import load_gallerydl_config, extractor_key_for_source
    from app.services.redis_client import get_redis

    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

    result = await db.execute(select(Work).order_by(Work.created_at.desc()))
    all_works = result.scalars().all()
    total = len(all_works)
    if total == 0:
        return {"status": "ok", "message": "No works to rebuild", "rebuilt": 0, "errors": 0}

    r = get_redis()
    progress_key = "library:rebuild:progress"
    r.setex(progress_key, 3600, json.dumps({"current": 0, "total": total, "status": "running"}))

    file_index = FileIndex(os.path.join(str(settings.download_root), ".file-index.sqlite3"))
    config = load_gallerydl_config()
    rebuilt = 0
    errors = 0

    for idx, work in enumerate(all_works):
        try:
            ws_result = await db.execute(select(WorkSource).where(WorkSource.work_id == work.id))
            ws = ws_result.scalar_one_or_none()
            if not ws:
                continue

            as_result = await db.execute(
                select(Asset, AssetSource).join(AssetSource, AssetSource.asset_id == Asset.id)
                .where(AssetSource.work_source_id == ws.id).order_by(Asset.file_name)
            )
            asset_rows = as_result.all()
            if not asset_rows:
                continue

            _ek = extractor_key_for_source(ws.source)
            _ec = config.get("extractor", {}).get(_ek, {})
            _dt = _ec.get("directory", [ws.source, "{id}"])
            _rparts = []
            for part in _dt:
                rp = part
                rm = ws.raw_metadata or {}
                if isinstance(rm, dict):
                    for k, v in rm.items():
                        if isinstance(v, dict):
                            for sk, sv in v.items():
                                rp = rp.replace(f"{{user[{sk}]}}", str(sv) if sv else "")
                        elif isinstance(v, (str, int, float)):
                            rp = rp.replace(f"{{{k}}}", str(v) if v is not None else "")
                rp = rp.replace("{id}", ws.source_work_id)
                _rparts.append(rp.strip().replace("/", "_"))
            creator_dir = _rparts[1] if len(_rparts) > 1 else ws.source_work_id

            lib_dir = Path(settings.library_root) / ws.source / creator_dir / ws.source_work_id
            lib_dir.mkdir(parents=True, exist_ok=True)

            sc_result = await db.execute(
                select(SourceCreator).where(
                    SourceCreator.source_creator_id == ws.source_creator_id,
                    SourceCreator.source == ws.source,
                )
            ) if ws.source_creator_id else None
            sc = sc_result.scalar_one_or_none() if sc_result else None
            display_name = sc.display_name if sc else creator_dir

            assets_meta = []
            for asset, _as in asset_rows:
                assets_meta.append({"file_name": asset.file_name})
                fp = Path(settings.download_root) / asset.file_path
                if fp.exists() and fp.suffix.lower() in IMAGE_EXTS:
                    tp = await asyncio.to_thread(generate_thumbnail, str(fp), lib_dir, f"{fp.stem}.thumbnail")
                    if tp:
                        rel = str(Path(tp).relative_to(settings.library_root))
                        file_index.upsert(file_path=rel, storage_root="library", source=ws.source,
                                          creator_dir=creator_dir, work_id=ws.source_work_id,
                                          file_name=Path(tp).name, file_type="thumbnail",
                                          file_size=Path(tp).stat().st_size)

            with open(lib_dir / "metadata.json", "w") as mf:
                json.dump({
                    "work_id": str(work.id), "source": ws.source,
                    "source_work_id": ws.source_work_id, "title": work.title,
                    "posted_at": work.posted_at.isoformat() if work.posted_at else None,
                    "creator": display_name, "assets": assets_meta,
                }, mf, indent=2, ensure_ascii=False, default=str)

            file_index.upsert(
                file_path=str(Path(ws.source) / creator_dir / ws.source_work_id / "metadata.json"),
                storage_root="library", source=ws.source, creator_dir=creator_dir,
                work_id=ws.source_work_id, file_name="metadata.json",
                file_type="metadata_json", file_size=(lib_dir / "metadata.json").stat().st_size,
                import_status="done",
            )
            rebuilt += 1
        except Exception:
            errors += 1
            logger.warning("Library rebuild: failed work %s", work.id, exc_info=True)

        if idx % 50 == 0:
            r.setex(progress_key, 3600, json.dumps({"current": idx + 1, "total": total, "status": "running"}))

    r.setex(progress_key, 3600, json.dumps({"current": total, "total": total, "status": "done", "rebuilt": rebuilt, "errors": errors}))
    return {"status": "ok", "message": f"Rebuilt {rebuilt} works ({errors} errors)", "rebuilt": rebuilt, "errors": errors}
```

- [ ] **Step 2: Add API endpoint in admin.py**

Before `return router`:
```python
@router.post("/library/rebuild")
async def rebuild_library(db: AsyncSession = Depends(get_db)):
    """Rebuild /library/ metadata.json and thumbnails from database records."""
    return await admin_data.rebuild_library_index(db)
```

- [ ] **Step 3: Verify syntax and commit**

```bash
python3 -c "import ast; ast.parse(open('backend/app/services/admin_data.py').read()); ast.parse(open('backend/app/api/admin.py').read()); print('OK')"
git add backend/app/services/admin_data.py backend/app/api/admin.py
git commit -m "feat: add library rebuild — regenerate metadata.json + thumbnails from DB"
```

---

### Task 2: Frontend — Data Management Action

**Files:**
- Modify: `admin-web/src/lib/api/index.ts` — add `rebuildLibrary` method
- Modify: `admin-web/src/app/admin/settings/data-mgmt/page.tsx` — add action card
- Modify: `admin-web/src/lib/i18n.tsx` — add zh+en keys

- [ ] **Step 1: API method** in `index.ts`:
```typescript
rebuildLibrary: () =>
  request<{ status: string; message: string; rebuilt: number; errors: number }>("/api/v1/admin/library/rebuild", { method: "POST" }),
```

- [ ] **Step 2: Mutation + action** in `data-mgmt/page.tsx`:
```typescript
const rebuildLibrary = useMutation({
  mutationFn: () => api.rebuildLibrary(),
  onSuccess: (d) => { toast.success({ message: d.message }); clearCacheThenRefetch(); setConfirmAction(null); },
  onError: (e) => { toast.error({ message: (e as Error).message }); setConfirmAction(null); },
});
// In actions array:
{ key: "rebuild-library", title: t("datamgmt.rebuild_library"), desc: t("datamgmt.rebuild_library.desc"), color: "blue", mutation: rebuildLibrary },
```

- [ ] **Step 3: i18n** zh: `"datamgmt.rebuild_library": "重建库索引"` / en: `"Rebuild Library Index"`

- [ ] **Step 4: Commit**

```bash
git add admin-web/
git commit -m "feat: add library rebuild action to data management page"
```

---

### Task 3: Build and Verify

- [ ] **Step 1: Build + restart**

```bash
docker compose build backend admin-web
docker compose up -d --force-recreate backend admin-web
```

- [ ] **Step 2: Test the endpoint**

```bash
# After containers healthy, trigger rebuild via admin UI or curl
```

- [ ] **Step 3: Verify regenerated files**

```bash
find /volume2/docker/auto-gallery/data/library -name "metadata.json" | head -5
```

- [ ] **Step 4: Final commit**

```bash
git commit -m "chore: library rebuild feature complete"
```
