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
