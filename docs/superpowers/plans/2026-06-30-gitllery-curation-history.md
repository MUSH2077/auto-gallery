# Gitllery On-Disk Curation History — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mirror the authoritative Postgres curation DAG into a per-source-creator, git-isomorphic `.gitllery/` directory on disk, written after every curation commit, with a `status`/verify and `reconcile`/catch-up path.

**Architecture:** A new read-only-on-DB `GitlleryService` projects each `curation_commits` row (sliced per affected source-creator) into `LIBRARY_ROOT/{source}/{creator_dir}/.gitllery/`. Objects are sha256-addressed, zlib-compressed canonical JSON (blob = entity state, tree = worktree manifest, commit = curation commit). Projection is best-effort after the caller's `db.commit()`; content-addressing + an idempotent catch-up make it crash-safe. DB stays the source of truth.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 async, FastAPI, Pydantic v2, pytest; Next.js 14 / React 18 / TanStack Query / Tailwind for the admin panel.

## Global Constraints

- DB is authoritative; `.gitllery` is a one-way projection. `CurationService` write methods are NOT modified.
- Repo boundary: one repo per source-creator at `LIBRARY_ROOT/{source}/{creator_dir}/.gitllery/`.
- Object addressing: `sha256` of canonical JSON (`json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))` → UTF-8). Stored zlib-compressed at `objects/{hash[:2]}/{hash[2:]}`.
- All filesystem writes atomic: temp file + `os.fsync` + `os.replace`. Never partial.
- Path containment: every `.gitllery` path validated with `Path.relative_to(LIBRARY_ROOT)`, never string-prefix matching.
- No NAS path leakage: APIs return hashes, relative paths, and counts only — never `/library/...` absolute paths.
- No shell: pure Python file IO; never shell out to `git`.
- Projection is best-effort: `try/except → logger.warning(exc_info=True)`; a user action never fails because projection failed.
- Projection performs NO writes to DB (pure read + filesystem), so it cannot corrupt the caller's transaction.
- Concurrency: per-repo projection serialized with `redis_lock(f"gitllery:{repository_id}", ttl_seconds=60)`.
- All new API endpoints require `RequireAdmin`.
- Tests run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest <args> -v`. DB tests use `@pytest.mark.integration @pytest.mark.asyncio` and `app.database.async_session` directly (see `backend/tests/test_account_events.py`); pure-filesystem tests use `tmp_path`.
- Commit after each task. End every commit message with `Co-Authored-By: Claude <noreply@anthropic.com>`.

## File Structure

| Path | Responsibility |
|---|---|
| `backend/app/services/gitllery/__init__.py` | Package exports (`GitlleryService`, `project_commit_safe`) |
| `backend/app/services/gitllery/objects.py` | Content-addressed object store (canonical JSON, sha256, zlib, atomic, sharded). Pure; no DB. |
| `backend/app/services/gitllery/repo.py` | Per-repo filesystem layer: init, HEAD/refs, reflog, index, config, containment. Pure; no DB. |
| `backend/app/services/gitllery/slicing.py` | `RepoResolver` + `RepoDescriptor`: map a commit's changes → affected repos. Reads DB. |
| `backend/app/services/gitllery/service.py` | `GitlleryService`: blobs/tree/commit objects, `project_commit`, `project_pending`, `backfill`, `status`, `reconcile`, `log`, `project_commit_safe`. |
| `backend/app/schemas/gitllery.py` | Pydantic response schemas. |
| `backend/app/api/curation.py` (modify) | Add 5 gitllery routes to the existing curation router. |
| `backend/app/api/works.py`, `creators.py`, `curation.py`, `jobs/import_runner.py` (modify) | Best-effort `project_commit_safe(...)` after existing commits. |
| `backend/tests/test_gitllery_objects.py` | Object store unit tests. |
| `backend/tests/test_gitllery_repo.py` | Repo FS unit tests. |
| `backend/tests/test_gitllery_slicing.py` | Slicing integration tests. |
| `backend/tests/test_gitllery_service.py` | Projection/status/catch-up integration tests. |
| `backend/tests/test_gitllery_api.py` | Endpoint integration tests. |
| `admin-web/src/lib/api.ts` (modify) | Typed client methods. |
| `admin-web/src/components/GitlleryPanel.tsx` | Status badge + reconcile + commit timeline. |
| `admin-web/src/lib/i18n*` (modify) | i18n keys. |
| `docs/architecture.md`, `docs/architecture.zh.md` (modify) | Document the gitllery projection. |

---

### Task 1: Content-addressed object store

**Files:**
- Create: `backend/app/services/gitllery/__init__.py`
- Create: `backend/app/services/gitllery/objects.py`
- Test: `backend/tests/test_gitllery_objects.py`

**Interfaces:**
- Produces: `canonical_bytes(payload: dict) -> bytes`, `hash_payload(payload: dict) -> str`, `class ObjectStore(objects_dir: Path)` with `.write(payload) -> str`, `.read(digest) -> dict`, `.exists(digest) -> bool`, `.verify(digest) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_gitllery_objects.py
from pathlib import Path

from app.services.gitllery.objects import ObjectStore, canonical_bytes, hash_payload


def test_canonical_bytes_is_key_order_independent():
    a = {"b": 1, "a": 2, "nested": {"y": 1, "x": 2}}
    b = {"a": 2, "nested": {"x": 2, "y": 1}, "b": 1}
    assert canonical_bytes(a) == canonical_bytes(b)
    assert hash_payload(a) == hash_payload(b)


def test_write_is_idempotent_and_sharded(tmp_path: Path):
    store = ObjectStore(tmp_path / "objects")
    payload = {"type": "blob", "state": {"visibility": "trashed"}}
    digest = store.write(payload)
    again = store.write(payload)
    assert digest == again
    assert len(digest) == 64
    assert (tmp_path / "objects" / digest[:2] / digest[2:]).exists()


def test_round_trip_and_verify(tmp_path: Path):
    store = ObjectStore(tmp_path / "objects")
    payload = {"type": "commit", "message": "trash 3 works", "n": 3}
    digest = store.write(payload)
    assert store.read(digest) == payload
    assert store.exists(digest) is True
    assert store.verify(digest) is True


def test_verify_detects_corruption(tmp_path: Path):
    store = ObjectStore(tmp_path / "objects")
    digest = store.write({"type": "blob", "x": 1})
    target = tmp_path / "objects" / digest[:2] / digest[2:]
    target.write_bytes(b"corrupted-not-zlib")
    assert store.verify(digest) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest backend/tests/test_gitllery_objects.py -v`
Expected: FAIL with `ModuleNotFoundError: app.services.gitllery.objects`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/gitllery/__init__.py
"""Gitllery — on-disk, git-isomorphic projection of the curation DAG."""
```

```python
# backend/app/services/gitllery/objects.py
"""Content-addressed object store for .gitllery repositories.

Objects are zlib-compressed canonical JSON, addressed by sha256 of the
uncompressed canonical bytes, stored under objects/{hash[:2]}/{hash[2:]}
(git-style sharding). Pure filesystem + hashing; no database access.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
import zlib
from pathlib import Path


def canonical_bytes(payload: dict) -> bytes:
    """Deterministic JSON encoding: sorted keys, compact separators, UTF-8."""
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def hash_payload(payload: dict) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


class ObjectStore:
    def __init__(self, objects_dir: Path):
        self._dir = Path(objects_dir)

    def _path_for(self, digest: str) -> Path:
        return self._dir / digest[:2] / digest[2:]

    def exists(self, digest: str) -> bool:
        return self._path_for(digest).exists()

    def write(self, payload: dict) -> str:
        raw = canonical_bytes(payload)
        digest = hashlib.sha256(raw).hexdigest()
        target = self._path_for(digest)
        if target.exists():
            return digest
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".{digest}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(zlib.compress(raw))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        return digest

    def read(self, digest: str) -> dict:
        with open(self._path_for(digest), "rb") as fh:
            return json.loads(zlib.decompress(fh.read()).decode("utf-8"))

    def verify(self, digest: str) -> bool:
        try:
            raw = canonical_bytes(self.read(digest))
        except (OSError, ValueError, zlib.error):
            return False
        return hashlib.sha256(raw).hexdigest() == digest
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest backend/tests/test_gitllery_objects.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/gitllery/__init__.py backend/app/services/gitllery/objects.py backend/tests/test_gitllery_objects.py
git commit -m "feat(gitllery): content-addressed object store

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Per-repo filesystem layer

**Files:**
- Create: `backend/app/services/gitllery/repo.py`
- Test: `backend/tests/test_gitllery_repo.py`

**Interfaces:**
- Consumes: `ObjectStore` from Task 1.
- Produces: constants `GITLLERY_DIRNAME=".gitllery"`, `SCHEMA_VERSION=1`, `DEFAULT_BRANCH="main"`; `class GitlleryRepo(library_root, source, creator_dir)` with `.root: Path`, `.objects: ObjectStore`, `.exists()`, `.init(config: dict, description: str)`, `.head_commit() -> str|None`, `.set_head(new_commit, *, actor, message)`, `.read_reflog() -> list[dict]`, `.read_index() -> dict`, `.write_index(index)`, `.read_config() -> dict`, `.projected_db_commit_ids() -> set[str]`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_gitllery_repo.py
from pathlib import Path

import pytest

from app.services.gitllery.repo import GitlleryRepo, GITLLERY_DIRNAME


def _repo(tmp_path: Path) -> GitlleryRepo:
    return GitlleryRepo(tmp_path, "pixiv", "七诗")


def test_init_creates_git_isomorphic_layout(tmp_path: Path):
    repo = _repo(tmp_path)
    assert repo.exists() is False
    repo.init({"schema_version": 1, "source": "pixiv"}, "pixiv / 七诗 curation history")
    assert repo.exists() is True
    base = tmp_path / "pixiv" / "七诗" / GITLLERY_DIRNAME
    assert (base / "HEAD").read_text().strip() == "ref: refs/heads/main"
    assert (base / "objects").is_dir()
    assert (base / "refs" / "heads").is_dir()
    assert repo.read_config()["source"] == "pixiv"
    assert repo.head_commit() is None


def test_set_head_moves_ref_and_appends_reflog(tmp_path: Path):
    repo = _repo(tmp_path)
    repo.init({"schema_version": 1}, "d")
    repo.set_head("a" * 64, actor="admin", message="trash 1 work")
    assert repo.head_commit() == "a" * 64
    repo.set_head("b" * 64, actor="admin", message="restore 1 work")
    assert repo.head_commit() == "b" * 64
    log = repo.read_reflog()
    assert len(log) == 2
    assert log[0]["old"] == "0" * 64 and log[0]["new"] == "a" * 64
    assert log[1]["old"] == "a" * 64 and log[1]["new"] == "b" * 64
    assert log[1]["message"] == "restore 1 work"


def test_index_round_trip(tmp_path: Path):
    repo = _repo(tmp_path)
    repo.init({"schema_version": 1}, "d")
    assert repo.read_index() == {"head": None, "tree": None, "entities": {}}
    repo.write_index({"head": "c" * 64, "tree": "d" * 64, "entities": {"work/1": {"visibility": "trashed"}}})
    assert repo.read_index()["entities"]["work/1"]["visibility"] == "trashed"


def test_path_containment_rejects_escape(tmp_path: Path):
    with pytest.raises(ValueError):
        GitlleryRepo(tmp_path, "pixiv", "../../etc")


def test_projected_db_commit_ids_walks_chain(tmp_path: Path):
    repo = _repo(tmp_path)
    repo.init({"schema_version": 1}, "d")
    c1 = repo.objects.write({"type": "commit", "parent": None, "db_commit_id": "db-1"})
    c2 = repo.objects.write({"type": "commit", "parent": c1, "db_commit_id": "db-2"})
    repo.set_head(c2, actor="admin", message="m")
    assert repo.projected_db_commit_ids() == {"db-1", "db-2"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest backend/tests/test_gitllery_repo.py -v`
Expected: FAIL with `ModuleNotFoundError: app.services.gitllery.repo`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/gitllery/repo.py
"""Per-repository .gitllery filesystem layer (HEAD, refs, reflog, index, config).

Pure filesystem; no database. One GitlleryRepo == one
LIBRARY_ROOT/{source}/{creator_dir}/.gitllery directory.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.services.gitllery.objects import ObjectStore

GITLLERY_DIRNAME = ".gitllery"
SCHEMA_VERSION = 1
DEFAULT_BRANCH = "main"
_HEAD_REF = f"ref: refs/heads/{DEFAULT_BRANCH}"
_ZERO = "0" * 64


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class GitlleryRepo:
    def __init__(self, library_root, source: str, creator_dir: str):
        self._library_root = Path(library_root).resolve()
        creator_root = (self._library_root / source / creator_dir).resolve()
        self.root = (creator_root / GITLLERY_DIRNAME).resolve()
        # Path containment: .gitllery must stay under LIBRARY_ROOT.
        self.root.relative_to(self._library_root)  # raises ValueError on escape
        self.objects = ObjectStore(self.root / "objects")

    def exists(self) -> bool:
        return (self.root / "HEAD").exists()

    def init(self, config: dict, description: str) -> None:
        if self.exists():
            _atomic_write_text(self.root / "config.json",
                               json.dumps(config, indent=2, ensure_ascii=False))
            return
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        (self.root / "refs" / "heads").mkdir(parents=True, exist_ok=True)
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self.root / "HEAD", _HEAD_REF + "\n")
        _atomic_write_text(self.root / "description", description + "\n")
        _atomic_write_text(self.root / "config.json",
                           json.dumps(config, indent=2, ensure_ascii=False))

    def _ref_path(self) -> Path:
        return self.root / "refs" / "heads" / DEFAULT_BRANCH

    def head_commit(self) -> str | None:
        try:
            value = self._ref_path().read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    def set_head(self, new_commit: str, *, actor: str, message: str) -> None:
        old = self.head_commit() or _ZERO
        _atomic_write_text(self._ref_path(), new_commit + "\n")
        line = json.dumps(
            {"old": old, "new": new_commit, "actor": actor, "ts": _now_iso(), "message": message},
            ensure_ascii=False,
        ) + "\n"
        log_path = self.root / "logs" / "HEAD"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def read_reflog(self) -> list[dict]:
        try:
            text = (self.root / "logs" / "HEAD").read_text(encoding="utf-8")
        except OSError:
            return []
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def read_index(self) -> dict:
        try:
            return json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"head": None, "tree": None, "entities": {}}

    def write_index(self, index: dict) -> None:
        _atomic_write_text(self.root / "index.json",
                           json.dumps(index, sort_keys=True, ensure_ascii=False))

    def read_config(self) -> dict:
        try:
            return json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def projected_db_commit_ids(self) -> set[str]:
        seen: set[str] = set()
        cur = self.head_commit()
        guard = 0
        while cur and guard < 1_000_000:
            guard += 1
            try:
                obj = self.objects.read(cur)
            except OSError:
                break
            db_id = obj.get("db_commit_id")
            if db_id:
                seen.add(db_id)
            cur = obj.get("parent")
        return seen
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest backend/tests/test_gitllery_repo.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/gitllery/repo.py backend/tests/test_gitllery_repo.py
git commit -m "feat(gitllery): per-repo filesystem layer (HEAD/refs/reflog/index)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Commit → repository slicing

**Files:**
- Create: `backend/app/services/gitllery/slicing.py`
- Test: `backend/tests/test_gitllery_slicing.py`

**Interfaces:**
- Produces:
  - `@dataclass RepoDescriptor` fields `repository_id: str`, `source: str`, `source_creator_id: str|None`, `creator_id: str|None`, `creator_dir: str`; method `.key() -> str` (returns `repository_id`).
  - `class RepoResolver(db: AsyncSession)` with `async slice_changes(changes: list[CurationChange]) -> dict[str, tuple[RepoDescriptor, list[CurationChange]]]` and `async all_repositories() -> list[RepoDescriptor]`.
- `repository_id` = `str(subscription_source.id)` when a `subscription_source` exists for `(source, source_creator_id)`, else the synthetic `f"{source}:{source_creator_id}"` (mirrors `CurationService._baseline_work_groups`).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_gitllery_slicing.py
import uuid

import pytest
from sqlalchemy import text


async def _clear(db):
    await db.execute(text(
        "TRUNCATE curation_changes, curation_commits, asset_sources, assets, "
        "work_sources, works, source_creators, subscription_sources, subscriptions, "
        "creators RESTART IDENTITY CASCADE"))
    await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_slice_work_change_to_single_repo():
    from app.database import async_session
    from app.models import (Creator, SourceCreator, Work, WorkSource,
                             CurationChange)
    from app.services.gitllery.slicing import RepoResolver

    try:
        async with async_session() as db:
            await _clear(db)
            creator = Creator(name="七诗")
            db.add(creator)
            await db.flush()
            db.add(SourceCreator(creator_id=creator.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            work = Work(title="w")
            db.add(work)
            await db.flush()
            db.add(WorkSource(work_id=work.id, source="pixiv", source_work_id="9001",
                              source_creator_id="123", raw_metadata={"user": {"name": "七诗"}}))
            await db.commit()

            change = CurationChange(commit_id=uuid.uuid4(), subject_type="work",
                                    subject_id=str(work.id), action="work_trashed",
                                    before_state=None, after_state={"visibility": "trashed"})
            sliced = await RepoResolver(db).slice_changes([change])
            assert len(sliced) == 1
            desc, changes = next(iter(sliced.values()))
            assert desc.source == "pixiv"
            assert desc.source_creator_id == "123"
            assert desc.creator_id == str(creator.id)
            assert len(changes) == 1
    finally:
        async with async_session() as db:
            await _clear(db)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_slice_creator_change_fans_out_to_all_source_repos():
    from app.database import async_session
    from app.models import Creator, SourceCreator, CurationChange
    from app.services.gitllery.slicing import RepoResolver

    try:
        async with async_session() as db:
            await _clear(db)
            creator = Creator(name="七诗")
            db.add(creator)
            await db.flush()
            db.add(SourceCreator(creator_id=creator.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            db.add(SourceCreator(creator_id=creator.id, source="x",
                                 source_creator_id="abc", display_name="七诗"))
            await db.commit()

            change = CurationChange(commit_id=uuid.uuid4(), subject_type="creator",
                                    subject_id=str(creator.id), action="creator_archived",
                                    before_state=None, after_state={"visibility": "archived"})
            sliced = await RepoResolver(db).slice_changes([change])
            sources = sorted(desc.source for desc, _ in sliced.values())
            assert sources == ["pixiv", "x"]
    finally:
        async with async_session() as db:
            await _clear(db)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest backend/tests/test_gitllery_slicing.py -v`
Expected: FAIL with `ModuleNotFoundError: app.services.gitllery.slicing`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/gitllery/slicing.py
"""Map global curation changes to the per-source-creator .gitllery repos they affect."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AssetSource, CurationChange, SourceCreator, Subscription,
    SubscriptionSource, WorkSource,
)
from app.services.library_sync import resolve_creator_directory


@dataclass
class RepoDescriptor:
    repository_id: str
    source: str
    source_creator_id: str | None
    creator_id: str | None
    creator_dir: str

    def key(self) -> str:
        return self.repository_id


class RepoResolver:
    """Resolve entities (work/creator/asset/repository) to RepoDescriptors.

    Create one per projection pass; lookups are cached on the instance.
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self._repo_lookup: dict[tuple[str, str], SubscriptionSource] | None = None
        self._creator_by_subscription: dict[UUID, str | None] = {}
        self._creator_by_source_creator: dict[tuple[str, str], str | None] = {}

    async def _ensure_repo_lookup(self) -> dict[tuple[str, str], SubscriptionSource]:
        if self._repo_lookup is None:
            rows = await self.db.execute(
                select(SubscriptionSource)
                .where(SubscriptionSource.source_creator_id.isnot(None))
                .order_by(SubscriptionSource.created_at)
            )
            lookup: dict[tuple[str, str], SubscriptionSource] = {}
            for repo in rows.scalars().all():
                if repo.source_creator_id:
                    lookup.setdefault((repo.source, repo.source_creator_id), repo)
            self._repo_lookup = lookup
        return self._repo_lookup

    async def _creator_for_subscription(self, subscription_id: UUID) -> str | None:
        if subscription_id not in self._creator_by_subscription:
            row = await self.db.execute(
                select(Subscription.creator_id).where(Subscription.id == subscription_id))
            cid = row.scalar_one_or_none()
            self._creator_by_subscription[subscription_id] = str(cid) if cid else None
        return self._creator_by_subscription[subscription_id]

    async def _creator_for_source_creator(self, source: str, scid: str) -> str | None:
        key = (source, scid)
        if key not in self._creator_by_source_creator:
            row = await self.db.execute(
                select(SourceCreator.creator_id).where(
                    SourceCreator.source == source,
                    SourceCreator.source_creator_id == scid))
            cid = row.scalar_one_or_none()
            self._creator_by_source_creator[key] = str(cid) if cid else None
        return self._creator_by_source_creator[key]

    async def _descriptor_for_work_source(self, ws: WorkSource) -> RepoDescriptor | None:
        if not ws.source_creator_id:
            return None
        creator_dir = resolve_creator_directory(ws.source, ws.raw_metadata or {}, ws.source_work_id)
        lookup = await self._ensure_repo_lookup()
        repo = lookup.get((ws.source, ws.source_creator_id))
        if repo:
            repository_id = str(repo.id)
            creator_id = await self._creator_for_subscription(repo.subscription_id)
        else:
            repository_id = f"{ws.source}:{ws.source_creator_id}"
            creator_id = await self._creator_for_source_creator(ws.source, ws.source_creator_id)
        return RepoDescriptor(repository_id=repository_id, source=ws.source,
                              source_creator_id=ws.source_creator_id,
                              creator_id=creator_id, creator_dir=creator_dir)

    async def _descriptors_for_creator(self, creator_id: UUID) -> list[RepoDescriptor]:
        rows = await self.db.execute(
            select(SourceCreator).where(SourceCreator.creator_id == creator_id))
        out: list[RepoDescriptor] = []
        for sc in rows.scalars().all():
            ws_row = await self.db.execute(
                select(WorkSource).where(
                    WorkSource.source == sc.source,
                    WorkSource.source_creator_id == sc.source_creator_id).limit(1))
            ws = ws_row.scalar_one_or_none()
            creator_dir = resolve_creator_directory(
                sc.source, ws.raw_metadata if ws else {},
                ws.source_work_id if ws else (sc.source_creator_id or ""))
            lookup = await self._ensure_repo_lookup()
            repo = lookup.get((sc.source, sc.source_creator_id))
            repository_id = str(repo.id) if repo else f"{sc.source}:{sc.source_creator_id}"
            out.append(RepoDescriptor(repository_id=repository_id, source=sc.source,
                                      source_creator_id=sc.source_creator_id,
                                      creator_id=str(creator_id), creator_dir=creator_dir))
        return out

    async def _descriptors_for_asset(self, asset_id: UUID) -> list[RepoDescriptor]:
        rows = await self.db.execute(
            select(WorkSource).join(AssetSource, AssetSource.work_source_id == WorkSource.id)
            .where(AssetSource.asset_id == asset_id))
        out: list[RepoDescriptor] = []
        for ws in rows.scalars().unique().all():
            desc = await self._descriptor_for_work_source(ws)
            if desc:
                out.append(desc)
        return out

    async def _descriptors_for_repository(self, repository_id: str) -> list[RepoDescriptor]:
        try:
            repo_uuid = UUID(repository_id)
        except (ValueError, TypeError):
            return []
        row = await self.db.execute(
            select(SubscriptionSource).where(SubscriptionSource.id == repo_uuid))
        repo = row.scalar_one_or_none()
        if not repo or not repo.source_creator_id:
            return []
        ws_row = await self.db.execute(
            select(WorkSource).where(
                WorkSource.source == repo.source,
                WorkSource.source_creator_id == repo.source_creator_id).limit(1))
        ws = ws_row.scalar_one_or_none()
        creator_dir = resolve_creator_directory(
            repo.source, ws.raw_metadata if ws else {},
            ws.source_work_id if ws else (repo.source_creator_id or ""))
        creator_id = await self._creator_for_subscription(repo.subscription_id)
        return [RepoDescriptor(repository_id=str(repo.id), source=repo.source,
                               source_creator_id=repo.source_creator_id,
                               creator_id=creator_id, creator_dir=creator_dir)]

    async def _repos_for_change(self, change: CurationChange) -> list[RepoDescriptor]:
        st, sid = change.subject_type, change.subject_id
        if st == "work":
            rows = await self.db.execute(
                select(WorkSource).where(WorkSource.work_id == UUID(sid)))
            out = []
            for ws in rows.scalars().all():
                desc = await self._descriptor_for_work_source(ws)
                if desc:
                    out.append(desc)
            return out
        if st == "asset":
            return await self._descriptors_for_asset(UUID(sid))
        if st == "creator":
            return await self._descriptors_for_creator(UUID(sid))
        if st == "repository":
            return await self._descriptors_for_repository(sid)
        return []

    async def slice_changes(
        self, changes: list[CurationChange]
    ) -> dict[str, tuple[RepoDescriptor, list[CurationChange]]]:
        result: dict[str, tuple[RepoDescriptor, list[CurationChange]]] = {}
        for change in changes:
            for desc in await self._repos_for_change(change):
                if desc.key() not in result:
                    result[desc.key()] = (desc, [])
                result[desc.key()][1].append(change)
        return result

    async def all_repositories(self) -> list[RepoDescriptor]:
        rows = await self.db.execute(
            select(WorkSource.source, WorkSource.source_creator_id)
            .where(WorkSource.source_creator_id.isnot(None)).distinct())
        out: list[RepoDescriptor] = []
        seen: set[str] = set()
        for source, scid in rows.all():
            ws_row = await self.db.execute(
                select(WorkSource).where(
                    WorkSource.source == source,
                    WorkSource.source_creator_id == scid).limit(1))
            ws = ws_row.scalar_one_or_none()
            if not ws:
                continue
            desc = await self._descriptor_for_work_source(ws)
            if desc and desc.key() not in seen:
                seen.add(desc.key())
                out.append(desc)
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest backend/tests/test_gitllery_slicing.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/gitllery/slicing.py backend/tests/test_gitllery_slicing.py
git commit -m "feat(gitllery): slice global curation changes into per-repo subsets

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: GitlleryService — project a single commit

**Files:**
- Create: `backend/app/services/gitllery/service.py`
- Modify: `backend/app/services/gitllery/__init__.py`
- Test: `backend/tests/test_gitllery_service.py`

**Interfaces:**
- Consumes: `ObjectStore`, `GitlleryRepo`, `RepoResolver`/`RepoDescriptor`, `redis_lock`, `settings.library_root`.
- Produces: `class GitlleryService(db: AsyncSession)` with `async project_commit(commit_id: UUID) -> list[str]` (returns affected repository ids); `async _changes_for_commit(commit_id) -> list[CurationChange]`; and module fn `async project_commit_safe(db, commit_id) -> None`.
- Object payloads:
  - blob: `{"type":"blob","subject_type":..,"subject_id":..,"state":<after_state or {}>}`
  - tree: `{"type":"tree","entries": {"<subject_type>/<subject_id>": "<blobhash>"}}` (removals drop the entry)
  - commit: `{"type":"commit","tree":<treehash>,"parent":<hash|null>,"db_commit_id":str,"actor_type":..,"actor_id":..,"message":..,"trigger":..,"dedupe_key":..,"reverts":..,"occurred_at":iso,"stats":{..},"changes":[{"subject_type","subject_id","action","diff","impact"}]}`
- `index` keys: `head`, `tree`, `tree_entries` (the tree's `entries` map), `entities` (entity_key → after_state).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_gitllery_service.py
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text


async def _clear(db):
    await db.execute(text(
        "TRUNCATE curation_changes, curation_commits, asset_sources, assets, "
        "work_sources, works, source_creators, subscription_sources, subscriptions, "
        "creators RESTART IDENTITY CASCADE"))
    await db.commit()


async def _seed_work(db):
    from app.models import Creator, SourceCreator, Work, WorkSource
    creator = Creator(name="七诗")
    db.add(creator)
    await db.flush()
    db.add(SourceCreator(creator_id=creator.id, source="pixiv",
                         source_creator_id="123", display_name="七诗"))
    work = Work(title="w")
    db.add(work)
    await db.flush()
    db.add(WorkSource(work_id=work.id, source="pixiv", source_work_id="9001",
                      source_creator_id="123", raw_metadata={"user": {"name": "七诗"}}))
    await db.commit()
    return creator, work


@pytest.mark.integration
@pytest.mark.asyncio
async def test_project_commit_writes_objects_refs_index(tmp_path, monkeypatch):
    from app.database import async_session
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import GitlleryService
    from app.services.gitllery.repo import GitlleryRepo

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear(db)
            creator, work = await _seed_work(db)
            commit = await CurationService(db).trash_works([work.id], message="trash 1")
            await db.commit()

            affected = await GitlleryService(db).project_commit(commit.id)
            assert len(affected) == 1

            repo = GitlleryRepo(tmp_path, "pixiv", "七诗")
            assert repo.exists()
            head = repo.head_commit()
            assert head is not None
            commit_obj = repo.objects.read(head)
            assert commit_obj["type"] == "commit"
            assert commit_obj["db_commit_id"] == str(commit.id)
            assert commit_obj["parent"] is None
            index = repo.read_index()
            assert index["entities"][f"work/{work.id}"]["visibility"] == "trashed"

            # Idempotent: re-project does not advance the ref.
            await GitlleryService(db).project_commit(commit.id)
            assert repo.head_commit() == head
    finally:
        async with async_session() as db:
            await _clear(db)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest backend/tests/test_gitllery_service.py -v`
Expected: FAIL with `ImportError`/`ModuleNotFoundError` for `app.services.gitllery.service`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/gitllery/service.py
"""GitlleryService — project the authoritative curation DAG onto disk.

Read-only on the database; all mutation is filesystem. Best-effort and
idempotent: re-projecting a commit is a no-op, a missed projection is caught up.
"""
from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import CurationChange, CurationCommit
from app.services.gitllery.repo import GitlleryRepo, SCHEMA_VERSION
from app.services.gitllery.slicing import RepoDescriptor, RepoResolver
from app.services.locks import redis_lock

logger = logging.getLogger(__name__)


def _change_payload(change: CurationChange) -> dict:
    return {
        "subject_type": change.subject_type,
        "subject_id": change.subject_id,
        "action": change.action,
        "diff": change.diff or {},
        "impact": change.impact or {},
    }


class GitlleryService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _changes_for_commit(self, commit_id: UUID) -> list[CurationChange]:
        rows = await self.db.execute(
            select(CurationChange).where(CurationChange.commit_id == commit_id)
            .order_by(CurationChange.created_at, CurationChange.id))
        return list(rows.scalars().all())

    def _repo_for(self, desc: RepoDescriptor) -> GitlleryRepo:
        return GitlleryRepo(Path(settings.library_root), desc.source, desc.creator_dir)

    def _ensure_init(self, repo: GitlleryRepo, desc: RepoDescriptor) -> None:
        if repo.exists():
            return
        repo.init(
            {
                "schema_version": SCHEMA_VERSION,
                "repository_id": desc.repository_id,
                "source": desc.source,
                "creator_id": desc.creator_id,
                "source_creator_id": desc.source_creator_id,
                "creator_dir": desc.creator_dir,
                "object_hash": "sha256",
                "compression": "zlib",
            },
            f"{desc.source} / {desc.creator_dir} curation history",
        )

    def _apply_commit_to_repo(
        self, repo: GitlleryRepo, desc: RepoDescriptor,
        commit: CurationCommit, changes: list[CurationChange],
    ) -> str | None:
        """Write blobs+tree+commit for one repo. Returns new commit hash, or None if already projected."""
        self._ensure_init(repo, desc)
        if str(commit.id) in repo.projected_db_commit_ids():
            return None

        index = repo.read_index()
        entries: dict[str, str] = dict((index.get("tree_entries") or {}))
        for change in changes:
            entity_key = f"{change.subject_type}/{change.subject_id}"
            after = change.after_state
            if after is None:
                entries.pop(entity_key, None)
                index.setdefault("entities", {}).pop(entity_key, None)
            else:
                blob_hash = repo.objects.write(
                    {"type": "blob", "subject_type": change.subject_type,
                     "subject_id": change.subject_id, "state": after})
                entries[entity_key] = blob_hash
                index.setdefault("entities", {})[entity_key] = after

        tree_hash = repo.objects.write({"type": "tree", "entries": entries})
        commit_hash = repo.objects.write({
            "type": "commit",
            "tree": tree_hash,
            "parent": repo.head_commit(),
            "db_commit_id": str(commit.id),
            "actor_type": commit.actor_type,
            "actor_id": commit.actor_id,
            "message": commit.message,
            "trigger": commit.trigger,
            "dedupe_key": commit.dedupe_key,
            "reverts": str(commit.reverts_commit_id) if commit.reverts_commit_id else None,
            "occurred_at": commit.occurred_at.isoformat() if commit.occurred_at else None,
            "stats": commit.stats or {},
            "changes": [_change_payload(c) for c in changes],
        })
        actor = commit.actor_id or commit.actor_type or "system"
        repo.set_head(commit_hash, actor=actor, message=commit.message)
        index["head"] = commit_hash
        index["tree"] = tree_hash
        index["tree_entries"] = entries
        repo.write_index(index)
        return commit_hash

    async def project_commit(self, commit_id: UUID) -> list[str]:
        commit = await self.db.get(CurationCommit, commit_id)
        if commit is None:
            return []
        changes = await self._changes_for_commit(commit_id)
        sliced = await RepoResolver(self.db).slice_changes(changes)
        affected: list[str] = []
        for repository_id, (desc, repo_changes) in sliced.items():
            async with redis_lock(f"gitllery:{repository_id}", ttl_seconds=60) as acquired:
                if not acquired:
                    logger.warning("gitllery: could not lock repo %s; skipping (catch-up will retry)",
                                   repository_id)
                    continue
                repo = self._repo_for(desc)
                if self._apply_commit_to_repo(repo, desc, commit, repo_changes) is not None:
                    affected.append(repository_id)
        return affected


async def project_commit_safe(db: AsyncSession, commit_id) -> None:
    """Best-effort projection: never raises into the caller's request/job."""
    try:
        cid = commit_id if isinstance(commit_id, UUID) else UUID(str(commit_id))
        await GitlleryService(db).project_commit(cid)
    except Exception:
        logger.warning("gitllery projection failed for commit %s", commit_id, exc_info=True)
```

```python
# backend/app/services/gitllery/__init__.py  (replace contents)
"""Gitllery — on-disk, git-isomorphic projection of the curation DAG."""

from app.services.gitllery.service import GitlleryService, project_commit_safe

__all__ = ["GitlleryService", "project_commit_safe"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest backend/tests/test_gitllery_service.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/gitllery/service.py backend/app/services/gitllery/__init__.py backend/tests/test_gitllery_service.py
git commit -m "feat(gitllery): project a single curation commit to disk (idempotent)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Catch-up (`project_pending`) and backfill

**Files:**
- Modify: `backend/app/services/gitllery/service.py`
- Test: `backend/tests/test_gitllery_service.py` (add cases)

**Interfaces:**
- Produces on `GitlleryService`: `async project_pending(repository_id: str | None = None) -> dict[str, int]` (repo id → commits projected) and `async backfill() -> dict[str, int]`. Both iterate all `curation_commits` ordered by `(occurred_at, created_at, id)`, slice each, and project any whose `db_commit_id` is not yet in the target repo's chain — in order.

- [ ] **Step 1: Write the failing test (append to test_gitllery_service.py)**

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_project_pending_catches_up_missed_commit(tmp_path, monkeypatch):
    from app.database import async_session
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import GitlleryService
    from app.services.gitllery.repo import GitlleryRepo

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear(db)
            creator, work = await _seed_work(db)
            c1 = await CurationService(db).trash_works([work.id], message="trash 1")
            await db.commit()
            c2 = await CurationService(db).restore_works([work.id], message="restore 1")
            await db.commit()
            # Simulate a missed projection: only project the SECOND commit.
            await GitlleryService(db).project_commit(c2.id)

            repo = GitlleryRepo(tmp_path, "pixiv", "七诗")
            assert str(c1.id) not in repo.projected_db_commit_ids()

            result = await GitlleryService(db).project_pending()
            assert sum(result.values()) >= 1
            ids = repo.projected_db_commit_ids()
            assert {str(c1.id), str(c2.id)} <= ids
    finally:
        async with async_session() as db:
            await _clear(db)
```

Note: `_apply_commit_to_repo` always parents on the *current* HEAD, so a late `c1` is appended after `c2` — history stays append-only; the late commit's position reflects projection time, not DB time. The test asserts membership, not order (acceptable for v1 audit).

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest backend/tests/test_gitllery_service.py::test_project_pending_catches_up_missed_commit -v`
Expected: FAIL with `AttributeError: 'GitlleryService' object has no attribute 'project_pending'`.

- [ ] **Step 3: Write minimal implementation (add methods to GitlleryService)**

```python
    async def _ordered_commits(self) -> list[CurationCommit]:
        rows = await self.db.execute(
            select(CurationCommit).order_by(
                CurationCommit.occurred_at, CurationCommit.created_at, CurationCommit.id))
        return list(rows.scalars().all())

    async def project_pending(self, repository_id: str | None = None) -> dict[str, int]:
        resolver = RepoResolver(self.db)
        counts: dict[str, int] = {}
        # repo_id -> (GitlleryRepo, RepoDescriptor, projected db_commit_id set)
        repo_cache: dict[str, tuple[GitlleryRepo, RepoDescriptor, set[str]]] = {}
        for commit in await self._ordered_commits():
            changes = await self._changes_for_commit(commit.id)
            sliced = await resolver.slice_changes(changes)
            for rid, (desc, repo_changes) in sliced.items():
                if repository_id is not None and rid != repository_id:
                    continue
                if rid not in repo_cache:
                    repo = self._repo_for(desc)
                    self._ensure_init(repo, desc)
                    repo_cache[rid] = (repo, desc, repo.projected_db_commit_ids())
                repo, desc_cached, projected = repo_cache[rid]
                if str(commit.id) in projected:
                    continue
                async with redis_lock(f"gitllery:{rid}", ttl_seconds=60) as acquired:
                    if not acquired:
                        continue
                    new_hash = self._apply_commit_to_repo(repo, desc_cached, commit, repo_changes)
                if new_hash is not None:
                    projected.add(str(commit.id))
                    counts[rid] = counts.get(rid, 0) + 1
        return counts

    async def backfill(self) -> dict[str, int]:
        # Ensure every source-creator repo exists, then project all commits.
        resolver = RepoResolver(self.db)
        for desc in await resolver.all_repositories():
            self._ensure_init(self._repo_for(desc), desc)
        return await self.project_pending()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest backend/tests/test_gitllery_service.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/gitllery/service.py backend/tests/test_gitllery_service.py
git commit -m "feat(gitllery): catch-up projection (project_pending) and backfill

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: status / verify and reconcile

**Files:**
- Modify: `backend/app/services/gitllery/service.py`
- Create: `backend/app/schemas/gitllery.py`
- Test: `backend/tests/test_gitllery_service.py` (add cases)

**Interfaces:**
- Produces on `GitlleryService`: `async status(repository_id: str | None = None) -> dict` and `async reconcile(repository_id: str | None = None) -> dict`.
- `status` shape: `{"repositories": [{"repository_id","source","creator_dir","exists","behind","object_integrity_ok","drift":[keys],"clean"}], "missing_repos": int, "behind_total": int}`. Never includes absolute paths.
- `schemas/gitllery.py`: `GitlleryRepoStatus`, `GitlleryStatusResponse`, `GitlleryReconcileResponse`, `GitlleryLogEntry`, `GitlleryLogResponse`.

- [ ] **Step 1: Write the failing test (append)**

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_clean_after_projection_and_behind_before(tmp_path, monkeypatch):
    from app.database import async_session
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import GitlleryService

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear(db)
            creator, work = await _seed_work(db)
            commit = await CurationService(db).trash_works([work.id], message="trash 1")
            await db.commit()

            before = await GitlleryService(db).status()
            assert before["behind_total"] >= 1

            await GitlleryService(db).reconcile()
            after = await GitlleryService(db).status()
            assert after["behind_total"] == 0
            assert all(r["clean"] for r in after["repositories"])
            assert all(r["object_integrity_ok"] for r in after["repositories"])
    finally:
        async with async_session() as db:
            await _clear(db)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest backend/tests/test_gitllery_service.py::test_status_clean_after_projection_and_behind_before -v`
Expected: FAIL — `status`/`reconcile` not defined.

- [ ] **Step 3: Write minimal implementation**

Add to `GitlleryService`:

```python
    async def _pending_count_by_repo(self, repository_id: str | None) -> tuple[dict[str, int], dict[str, RepoDescriptor]]:
        resolver = RepoResolver(self.db)
        projected_cache: dict[str, set[str]] = {}
        repo_desc: dict[str, RepoDescriptor] = {}
        pending: dict[str, int] = {}
        for commit in await self._ordered_commits():
            changes = await self._changes_for_commit(commit.id)
            sliced = await resolver.slice_changes(changes)
            for rid, (desc, _changes) in sliced.items():
                if repository_id is not None and rid != repository_id:
                    continue
                repo_desc[rid] = desc
                if rid not in projected_cache:
                    repo = self._repo_for(desc)
                    projected_cache[rid] = repo.projected_db_commit_ids() if repo.exists() else set()
                if str(commit.id) not in projected_cache[rid]:
                    pending[rid] = pending.get(rid, 0) + 1
        for rid in repo_desc:
            pending.setdefault(rid, 0)
        return pending, repo_desc

    def _integrity_ok(self, repo: GitlleryRepo) -> bool:
        head = repo.head_commit()
        if head is None:
            return True
        if not repo.objects.verify(head):
            return False
        commit_obj = repo.objects.read(head)
        tree_hash = commit_obj.get("tree")
        if not tree_hash or not repo.objects.verify(tree_hash):
            return False
        for blob_hash in (repo.objects.read(tree_hash).get("entries") or {}).values():
            if not repo.objects.exists(blob_hash):
                return False
        return True

    async def _live_state(self, subject_type: str, subject_id: str) -> dict | None:
        from app.models import AssetStorageState, CreatorCurationState, WorkCurationState
        try:
            sid = UUID(subject_id)
        except (ValueError, TypeError):
            return None
        if subject_type == "work":
            row = await self.db.execute(
                select(WorkCurationState).where(WorkCurationState.work_id == sid))
            st = row.scalar_one_or_none()
            return {"visibility": st.visibility if st else "visible"}
        if subject_type == "creator":
            row = await self.db.execute(
                select(CreatorCurationState).where(CreatorCurationState.creator_id == sid))
            st = row.scalar_one_or_none()
            return {"visibility": st.visibility if st else "visible"}
        if subject_type == "asset":
            row = await self.db.execute(
                select(AssetStorageState).where(AssetStorageState.asset_id == sid))
            st = row.scalar_one_or_none()
            return {"storage_state": st.storage_state if st else "available"}
        return None

    async def _drift_keys(self, repo: GitlleryRepo) -> list[str]:
        index_entities = repo.read_index().get("entities", {}) if repo.exists() else {}
        drift: list[str] = []
        for entity_key, disk_state in index_entities.items():
            subject_type, _, subject_id = entity_key.partition("/")
            live = await self._live_state(subject_type, subject_id)
            if live is None:
                continue
            for field in ("visibility", "storage_state"):
                if field in live and disk_state.get(field) != live.get(field):
                    drift.append(entity_key)
                    break
        return drift

    async def status(self, repository_id: str | None = None) -> dict:
        pending, repo_desc = await self._pending_count_by_repo(repository_id)
        # Include repos that exist on disk / in DB even with zero history.
        for desc in await RepoResolver(self.db).all_repositories():
            if repository_id is None or desc.key() == repository_id:
                repo_desc.setdefault(desc.key(), desc)
        repos_out = []
        missing = 0
        behind_total = 0
        for rid, desc in repo_desc.items():
            repo = self._repo_for(desc)
            exists = repo.exists()
            if not exists:
                missing += 1
            behind = pending.get(rid, 0)
            behind_total += behind
            integrity = self._integrity_ok(repo) if exists else True
            drift = await self._drift_keys(repo) if exists else []
            repos_out.append({
                "repository_id": rid,
                "source": desc.source,
                "creator_dir": desc.creator_dir,
                "exists": exists,
                "behind": behind,
                "object_integrity_ok": integrity,
                "drift": drift,
                "clean": exists and behind == 0 and integrity and not drift,
            })
        return {"repositories": repos_out, "missing_repos": missing, "behind_total": behind_total}

    async def reconcile(self, repository_id: str | None = None) -> dict:
        projected = await self.project_pending(repository_id)
        return {"projected": projected, "status": await self.status(repository_id)}
```

Create schemas:

```python
# backend/app/schemas/gitllery.py
from pydantic import BaseModel, Field


class GitlleryRepoStatus(BaseModel):
    repository_id: str
    source: str
    creator_dir: str
    exists: bool
    behind: int
    object_integrity_ok: bool
    drift: list[str] = Field(default_factory=list)
    clean: bool


class GitlleryStatusResponse(BaseModel):
    repositories: list[GitlleryRepoStatus]
    missing_repos: int
    behind_total: int


class GitlleryReconcileResponse(BaseModel):
    projected: dict[str, int]
    status: GitlleryStatusResponse


class GitlleryLogEntry(BaseModel):
    commit: str
    db_commit_id: str | None = None
    message: str
    trigger: str | None = None
    actor: str | None = None
    occurred_at: str | None = None
    change_count: int = 0


class GitlleryLogResponse(BaseModel):
    repository_id: str
    entries: list[GitlleryLogEntry]
    total: int
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest backend/tests/test_gitllery_service.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/gitllery/service.py backend/app/schemas/gitllery.py backend/tests/test_gitllery_service.py
git commit -m "feat(gitllery): status/verify (behind, integrity, drift) and reconcile

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: API endpoints + log

**Files:**
- Modify: `backend/app/services/gitllery/service.py` (add `log`)
- Modify: `backend/app/api/curation.py`
- Test: `backend/tests/test_gitllery_api.py`

**Interfaces:**
- Produces on `GitlleryService`: `async log(repository_id: str, limit: int = 50) -> dict` walking the repo chain from HEAD into `GitlleryLogResponse` shape.
- Routes (inherit the curation router's prefix + `RequireAdmin`):
  - `GET  /gitllery/status` → `GitlleryStatusResponse`
  - `GET  /repositories/{repository_id}/gitllery/status` → `GitlleryStatusResponse`
  - `POST /gitllery/reconcile` (optional `?repository_id=`) → `GitlleryReconcileResponse`
  - `POST /gitllery/backfill` → `GitlleryReconcileResponse`
  - `GET  /repositories/{repository_id}/gitllery/log` → `GitlleryLogResponse`

- [ ] **Step 1: Verify the curation router prefix, then write the failing test**

Run: `grep -rn "curation" backend/app/api/router.py` to confirm the mount prefix (expected `/api/v1/curation`). Adjust the test URLs below if it differs.

```python
# backend/tests/test_gitllery_api.py
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text


async def _clear(db):
    await db.execute(text(
        "TRUNCATE curation_changes, curation_commits, work_sources, works, "
        "source_creators, creators RESTART IDENTITY CASCADE"))
    await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_and_reconcile_endpoints(tmp_path, monkeypatch):
    from app.database import async_session
    from app.main import app
    from app.auth import RequireAdmin
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.models import Creator, SourceCreator, Work, WorkSource

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    app.dependency_overrides[RequireAdmin] = lambda: {"sub": "admin"}
    try:
        async with async_session() as db:
            await _clear(db)
            creator = Creator(name="七诗"); db.add(creator); await db.flush()
            db.add(SourceCreator(creator_id=creator.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            work = Work(title="w"); db.add(work); await db.flush()
            db.add(WorkSource(work_id=work.id, source="pixiv", source_work_id="9001",
                              source_creator_id="123", raw_metadata={"user": {"name": "七诗"}}))
            await db.commit()
            await CurationService(db).trash_works([work.id], message="trash 1")
            await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/v1/curation/gitllery/status")
            assert r.status_code == 200
            assert r.json()["behind_total"] >= 1

            r = await client.post("/api/v1/curation/gitllery/reconcile")
            assert r.status_code == 200
            assert r.json()["status"]["behind_total"] == 0
    finally:
        app.dependency_overrides.pop(RequireAdmin, None)
        async with async_session() as db:
            await _clear(db)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest backend/tests/test_gitllery_api.py -v`
Expected: FAIL with 404 (routes not yet defined).

- [ ] **Step 3: Write minimal implementation**

Add `log` to `GitlleryService`:

```python
    async def log(self, repository_id: str, limit: int = 50) -> dict:
        resolver = RepoResolver(self.db)
        desc = next((d for d in await resolver.all_repositories() if d.key() == repository_id), None)
        if desc is None:
            return {"repository_id": repository_id, "entries": [], "total": 0}
        repo = self._repo_for(desc)
        entries = []
        cur = repo.head_commit()
        total = 0
        guard = 0
        while cur and guard < 1_000_000:
            guard += 1
            try:
                obj = repo.objects.read(cur)
            except OSError:
                break
            total += 1
            if len(entries) < limit:
                entries.append({
                    "commit": cur,
                    "db_commit_id": obj.get("db_commit_id"),
                    "message": obj.get("message", ""),
                    "trigger": obj.get("trigger"),
                    "actor": obj.get("actor_id") or obj.get("actor_type"),
                    "occurred_at": obj.get("occurred_at"),
                    "change_count": len(obj.get("changes") or []),
                })
            cur = obj.get("parent")
        return {"repository_id": repository_id, "entries": entries, "total": total}
```

Add routes to `backend/app/api/curation.py` (append; extend imports — `Query` is already imported):

```python
from app.schemas.gitllery import (
    GitlleryLogResponse, GitlleryReconcileResponse, GitlleryStatusResponse,
)
from app.services.gitllery import GitlleryService


@router.get("/gitllery/status", response_model=GitlleryStatusResponse)
async def gitllery_status(db: AsyncSession = Depends(get_db)):
    return await GitlleryService(db).status()


@router.get("/repositories/{repository_id}/gitllery/status", response_model=GitlleryStatusResponse)
async def gitllery_repo_status(repository_id: str, db: AsyncSession = Depends(get_db)):
    return await GitlleryService(db).status(repository_id)


@router.post("/gitllery/reconcile", response_model=GitlleryReconcileResponse)
async def gitllery_reconcile(repository_id: str | None = None, db: AsyncSession = Depends(get_db)):
    return await GitlleryService(db).reconcile(repository_id)


@router.post("/gitllery/backfill", response_model=GitlleryReconcileResponse)
async def gitllery_backfill(db: AsyncSession = Depends(get_db)):
    svc = GitlleryService(db)
    projected = await svc.backfill()
    return {"projected": projected, "status": await svc.status()}


@router.get("/repositories/{repository_id}/gitllery/log", response_model=GitlleryLogResponse)
async def gitllery_log(repository_id: str, limit: int = Query(50, ge=1, le=200),
                       db: AsyncSession = Depends(get_db)):
    return await GitlleryService(db).log(repository_id, limit)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest backend/tests/test_gitllery_api.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/gitllery/service.py backend/app/api/curation.py backend/tests/test_gitllery_api.py
git commit -m "feat(gitllery): status/reconcile/backfill/log API endpoints

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: Hook best-effort projection into curation write paths

**Files:**
- Modify: `backend/app/api/works.py` (favorite, trash single, trash batch, batch curate)
- Modify: `backend/app/api/creators.py` (curate_creator)
- Modify: `backend/app/api/curation.py` (purge, revert)
- Modify: `backend/app/jobs/import_runner.py` (record_imported_work path)
- Test: `backend/tests/test_gitllery_service.py` (add best-effort-resilience case)

**Interfaces:**
- Consumes: `project_commit_safe(db, commit_id)` from Task 4. Call it AFTER each existing `db.commit()`, passing the produced commit's id. It never raises.

- [ ] **Step 1: Write the failing test (append)**

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_projection_failure_does_not_break_caller(tmp_path, monkeypatch):
    from app.database import async_session
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import project_commit_safe, GitlleryService

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))

    async def _boom(self, commit_id):
        raise RuntimeError("disk full")
    monkeypatch.setattr(GitlleryService, "project_commit", _boom)

    try:
        async with async_session() as db:
            await _clear(db)
            creator, work = await _seed_work(db)
            commit = await CurationService(db).trash_works([work.id], message="trash 1")
            await db.commit()
            # Best-effort wrapper must swallow the failure (no raise).
            await project_commit_safe(db, commit.id)

        # Recovery: real projector + catch-up makes status clean.
        monkeypatch.undo()
        monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
        async with async_session() as db:
            await GitlleryService(db).reconcile()
            assert (await GitlleryService(db).status())["behind_total"] == 0
    finally:
        async with async_session() as db:
            await _clear(db)
```

- [ ] **Step 2: Run test to verify it passes the swallow contract**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest backend/tests/test_gitllery_service.py::test_projection_failure_does_not_break_caller -v`
Expected: PASS (the wrapper from Task 4 already swallows). This locks the contract before wiring routes; production wiring follows in Step 3.

- [ ] **Step 3: Wire `project_commit_safe` into each write path**

First check which service methods self-commit so you don't double-commit:
Run: `grep -n "self.db.commit\|await db.commit" backend/app/services/curation.py backend/app/api/works.py backend/app/api/creators.py backend/app/api/curation.py`

In `backend/app/api/works.py` add `from app.services.gitllery import project_commit_safe`, then after each existing `await db.commit()` that followed a curation call, add `await project_commit_safe(db, commit.id)`:
- favorite route (after line ~88): `commit` from `record_work_favorite`.
- single trash route (~137): capture `commit = await svc.trash_works(...)`, commit, then project.
- batch trash / batch curate (after the `await db.commit()` near line ~210): use the `commit` produced at lines ~148/162/164.

In `backend/app/api/creators.py` add the import; after the `await db.commit()` that follows `curate_creator` (line ~404): `await project_commit_safe(db, commit.id)`.

In `backend/app/api/curation.py` update purge and revert:

```python
from app.services.gitllery import project_commit_safe

@router.post("/purge", response_model=CurationCommitRead)
async def purge_trashed_works(data: PurgeRequest, db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    commit = await svc.purge(data.work_ids, message=data.message)
    await db.commit()
    await project_commit_safe(db, commit.id)
    return await svc.commit_payload(commit.id)


@router.post("/commits/{commit_id}/revert", response_model=CurationRevertResponse)
async def revert_curation_commit(commit_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    result = await svc.revert_commit(commit_id)
    await db.commit()
    if result.get("commit") is not None:
        await project_commit_safe(db, result["commit"].id)
    return result
```

(If `purge`/`revert_commit`/`curate_creator` already call `self.db.commit()` internally — confirmed for `run_backfill` at line 627 — then drop the extra `await db.commit()` and keep only `project_commit_safe`. The grep in this step tells you which.)

In `backend/app/jobs/import_runner.py` add the import; capture the curation commit returned by `record_imported_work` and project it after the import transaction commits (near line ~635):

```python
from app.services.gitllery import project_commit_safe
# ... after the imported work's curation commit is committed:
        if curation_commit is not None:
            await project_commit_safe(db, curation_commit.id)
```

(`record_imported_work` returns `(commit, visibility)` per `curation.py:347`; bind the commit where the call result is used at line ~636.)

- [ ] **Step 4: Run the gitllery suite**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest backend/tests/test_gitllery_service.py backend/tests/test_gitllery_api.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/works.py backend/app/api/creators.py backend/app/api/curation.py backend/app/jobs/import_runner.py backend/tests/test_gitllery_service.py
git commit -m "feat(gitllery): project curation commits best-effort after write paths

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 9: Admin-web Gitllery panel

**Files:**
- Modify: `admin-web/src/lib/api.ts` (client methods)
- Create: `admin-web/src/components/GitlleryPanel.tsx`
- Modify: creator detail page to mount the panel (`admin-web/src/app/admin/creators/[id]/page.tsx`)
- Modify: i18n locale files (`admin-web/src/lib/i18n*`) — natural-language keys

**Interfaces:**
- Consumes: the Task 7 endpoints. First read `admin-web/src/lib/api.ts` to match the existing `request`/`api` helper and `queryKeys` style, then add methods returning typed results.

- [ ] **Step 1: Read existing client + a sibling panel for patterns**

Run: `sed -n '1,80p' admin-web/src/lib/api.ts` and open `admin-web/src/components/NotificationCenter.tsx` to mirror the `useQuery` + token-color conventions and the request helper signature.

- [ ] **Step 2: Add typed client methods to `api.ts`**

```ts
export interface GitlleryRepoStatus {
  repository_id: string; source: string; creator_dir: string;
  exists: boolean; behind: number; object_integrity_ok: boolean;
  drift: string[]; clean: boolean;
}
export interface GitlleryStatus {
  repositories: GitlleryRepoStatus[]; missing_repos: number; behind_total: number;
}

// methods on the `api` object — match the file's existing request() helper signature:
gitlleryStatus: () => request<GitlleryStatus>("/curation/gitllery/status"),
gitlleryReconcile: (repositoryId?: string) =>
  request<{ projected: Record<string, number>; status: GitlleryStatus }>(
    `/curation/gitllery/reconcile${repositoryId ? `?repository_id=${encodeURIComponent(repositoryId)}` : ""}`,
    { method: "POST" }),
gitlleryLog: (repositoryId: string) =>
  request<{ repository_id: string; entries: Array<Record<string, unknown>>; total: number }>(
    `/curation/repositories/${encodeURIComponent(repositoryId)}/gitllery/log`),
```

- [ ] **Step 3: Create `GitlleryPanel.tsx`**

```tsx
"use client";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useToast } from "@/components/Toast";

export default function GitlleryPanel() {
  const t = useT();
  const toast = useToast();
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["gitllery", "status"], queryFn: () => api.gitlleryStatus() });
  const reconcile = useMutation({
    mutationFn: () => api.gitlleryReconcile(),
    onSuccess: () => { toast.success(t("gitllery.reconciled")); qc.invalidateQueries({ queryKey: ["gitllery"] }); },
    onError: (e: Error) => toast.error(e.message),
  });

  if (status.isLoading) return <div className="h-20 animate-pulse rounded-md bg-subtle" />;
  if (status.error) return <div className="text-sm text-danger">{(status.error as Error).message}</div>;
  const s = status.data!;
  const behind = s.behind_total;
  const tone = behind === 0 ? "success" : "warning";

  return (
    <div className="card p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">{t("gitllery.title")}</h2>
        <span className={`badge bg-${tone}-subtle text-${tone}`}>
          {behind === 0 ? t("gitllery.clean") : t("gitllery.behind", { count: behind })}
        </span>
      </div>
      <div className="mt-2 text-xs text-muted">
        {t("gitllery.repos", { count: s.repositories.length })} · {t("gitllery.missing", { count: s.missing_repos })}
      </div>
      <button className="btn-ghost mt-3 px-3 py-1.5 text-xs" disabled={reconcile.isPending}
        onClick={() => reconcile.mutate()}>
        {reconcile.isPending ? t("common.saving") : t("gitllery.reconcile")}
      </button>
    </div>
  );
}
```

- [ ] **Step 4: Mount the panel + add i18n keys, then build**

Mount `<GitlleryPanel />` in the creator detail page sidebar. Add keys to each locale (natural-language): `gitllery.title` ("Curation History" / "策展历史"), `gitllery.clean`, `gitllery.behind`, `gitllery.repos`, `gitllery.missing`, `gitllery.reconcile`, `gitllery.reconciled`. The `bg-${tone}-subtle text-${tone}` classes are static-listed in `tailwind.config` safelist if Tailwind purges them — confirm `success`/`warning` subtle tokens are already used elsewhere (they are, post token-migration).

Run: `cd admin-web && npm run build`
Expected: build succeeds.

- [ ] **Step 5: Commit**

```bash
git add admin-web/src/lib/api.ts admin-web/src/components/GitlleryPanel.tsx "admin-web/src/app/admin/creators/[id]/page.tsx" admin-web/src/lib/i18n*
git commit -m "feat(admin-web): gitllery status panel with reconcile

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 10: Docs + full verification + deploy

**Files:**
- Modify: `docs/architecture.md`, `docs/architecture.zh.md`

- [ ] **Step 1: Document the projection (bilingual)**

Add a "Gitllery — On-Disk Curation History" subsection to both architecture docs: the per-source-creator `.gitllery/` layout, the object model (blob/tree/commit/refs/index/logs), that it is a one-way DB→disk projection written best-effort after each curation commit, and the `status`/`reconcile`/`backfill` operations. Code comments stay English-only; these docs are bilingual.

- [ ] **Step 2: Run the full backend suite**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest backend/tests/test_gitllery_objects.py backend/tests/test_gitllery_repo.py backend/tests/test_gitllery_slicing.py backend/tests/test_gitllery_service.py backend/tests/test_gitllery_api.py -v`
Expected: all gitllery tests pass. Then `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest` — no NEW failures vs the pre-existing baseline flake (`test_disk_import…idempotently`) noted in project history.

- [ ] **Step 3: Deploy (per CLAUDE.md completion workflow)**

```bash
docker compose build --build-arg CACHEBUST="$(date +%s)" --pull backend admin-web
docker compose up -d --force-recreate backend worker-download worker-import worker-operations scheduler admin-web
docker compose logs worker-import --tail=20
```

- [ ] **Step 4: Smoke-check the endpoint**

Trigger `POST /api/v1/curation/gitllery/backfill` once (admin JWT) to seed existing data, then `GET /api/v1/curation/gitllery/status` and confirm `behind_total: 0`. Inspect one `LIBRARY_ROOT/{source}/{creator_dir}/.gitllery/` on the NAS to confirm `HEAD`, `refs/heads/main`, `objects/`, `logs/HEAD`, `index.json`, `config.json` exist.

- [ ] **Step 5: Commit docs**

```bash
git add docs/architecture.md docs/architecture.zh.md
git commit -m "docs: document gitllery on-disk curation history projection

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:** every spec section maps to a task — object store (T1), layout/repo (T2), slicing (T3), projection flow + first-sync init (T4), catch-up + backfill (T5), status/verify + reconcile (T6), API + log (T7), best-effort write hooks (T8), frontend (T9), security/docs/testing (Global Constraints + T10). v2 items (reverse rebuild, outbox) are explicitly out of scope.

**Placeholder scan:** no TBD/TODO; every code step shows complete code. Two integration points are flagged to verify-before-edit (curation router prefix in T7 Step 1; whether `purge`/`revert`/`curate_creator` self-commit in T8 Step 3) — these are verification instructions, not placeholders.

**Type consistency:** `RepoDescriptor.key()` == `repository_id` used consistently across slicing/service/status; `project_commit` returns `list[str]`; `project_pending`/`backfill` return `dict[str,int]`; `_pending_count_by_repo` returns `(dict, dict)`; object payload keys (`type`/`db_commit_id`/`parent`/`tree`/`entries`/`changes`) consistent between writer (T4) and readers (`projected_db_commit_ids` T2, `_integrity_ok`/`log` T6/T7); `index` keys (`head`/`tree`/`tree_entries`/`entities`) consistent between T4 writer and T6 readers.

**Risk note:** slicing resolves `creator_dir` via `resolve_creator_directory(source, WorkSource.raw_metadata, source_work_id)`. For disk-imported works lacking metadata, that helper falls back to a safe segment, so the repo path stays deterministic — but in T4 Step 4 / T10 Step 4, confirm the projected `creator_dir` matches the actual on-disk library directory for at least one real creator before backfilling production, since a mismatch would create a sibling `.gitllery` next to the real library folder rather than inside it.
