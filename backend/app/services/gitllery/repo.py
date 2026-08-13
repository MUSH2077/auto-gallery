"""Per-repository .gitllery filesystem layer (HEAD, refs, reflog, index, config).

Pure filesystem; no database. One GitlleryRepo == one
LIBRARY_ROOT/{source}/{creator_dir}/.gitllery directory.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path

from app.services.gitllery.objects import ObjectStore, hash_payload

GITLLERY_DIRNAME = ".gitllery"
LEGACY_SCHEMA_VERSION = 1
SCHEMA_VERSION = 2
DEFAULT_BRANCH = "main"
_HEAD_REF = f"ref: refs/heads/{DEFAULT_BRANCH}"
_ZERO = "0" * 64
_V2_MANIFEST = Path("index-v2") / "manifest.json"
_V2_SHARD_HEX_CHARS = 2
_MIGRATION_OBJECT_BATCH = 256


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

    @contextmanager
    def projection_lock(self):
        """Process/host lock that remains authoritative if Redis TTL expires."""

        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / "projection.lock"
        with open(lock_path, "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def shard_for_entity(entity_key: str) -> str:
        return hashlib.sha256(entity_key.encode("utf-8")).hexdigest()[
            :_V2_SHARD_HEX_CHARS
        ]

    def read_projection_manifest(self) -> dict | None:
        try:
            manifest = json.loads(
                (self.root / _V2_MANIFEST).read_text(encoding="utf-8")
            )
            schema_version = int(manifest.get("schema_version") or 0)
        except (OSError, TypeError, ValueError):
            return None
        if schema_version != SCHEMA_VERSION:
            return None
        manifest.setdefault("shards", {})
        manifest.setdefault("generation", 0)
        return manifest

    def projection_manifest_for_update(self) -> dict:
        """Return a small v2 root without materialising a legacy flat index."""

        existing = self.read_projection_manifest()
        head = self.head_commit()
        if existing is not None and existing.get("head") == head:
            return existing
        head_object: dict = {}
        if head:
            try:
                head_object = self.objects.read(head)
            except (OSError, ValueError, zlib.error) as exc:
                raise ValueError("cannot resolve Gitllery HEAD") from exc
            if hash_payload(head_object) != head:
                raise ValueError("Gitllery HEAD failed content verification")
        projection = head_object.get("projection")
        if isinstance(projection, dict):
            # HEAD is the durable publication point. If a crash interrupted
            # the following manifest replace, its commit carries the complete
            # bounded root needed to resume without replaying history.
            return {**projection, "head": head}
        if existing is not None:
            # A legacy HEAD has no embedded v2 root. Preserve an existing
            # overlay rather than inventing an empty one.
            return existing
        legacy_index = self.root / "index.json"
        base = (
            {
                "schema_version": LEGACY_SCHEMA_VERSION,
                "index": "index.json",
                "tree": head_object.get("tree"),
            }
            if legacy_index.exists()
            else None
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "generation": 0,
            "head": head,
            "tree": head_object.get("tree"),
            "last_db_commit_id": head_object.get("db_commit_id"),
            "base": base,
            "shards": {},
        }

    def write_projection_manifest(self, manifest: dict) -> None:
        payload = {
            **manifest,
            "schema_version": SCHEMA_VERSION,
            "shards": dict(manifest.get("shards") or {}),
        }
        _atomic_write_text(
            self.root / _V2_MANIFEST,
            json.dumps(payload, sort_keys=True, ensure_ascii=False),
        )

    def recover_projection_manifest(
        self,
        commit_hash: str,
        commit_object: dict,
    ) -> bool:
        projection = commit_object.get("projection")
        if not isinstance(projection, dict):
            return False
        manifest = {**projection, "head": commit_hash}
        self.write_projection_manifest(manifest)
        return True

    def read_projection_shard(self, prefix: str, manifest: dict) -> dict:
        digest = (manifest.get("shards") or {}).get(prefix)
        if not digest:
            return {
                "type": "tree-shard-v2",
                "prefix": prefix,
                "entities": {},
                "entries": {},
            }
        payload = self.objects.read(digest)
        if payload.get("type") != "tree-shard-v2":
            raise ValueError(f"invalid legacy overlay shard {prefix}")
        return {
            **payload,
            "entities": dict(payload.get("entities") or {}),
            "entries": dict(payload.get("entries") or {}),
        }

    def _read_legacy_index(self) -> dict:
        try:
            return json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"head": None, "tree": None, "entities": {}}

    def read_index(self) -> dict:
        """Compatibility read: v1 flat index or a materialised v2 snapshot.

        Ordinary projection never calls this method. Materialising every shard
        is reserved for explicit status/deep-rebuild and legacy callers.
        """

        manifest = self.read_projection_manifest()
        if manifest is None:
            return self._read_legacy_index()
        index = (
            self._read_legacy_index()
            if manifest.get("base") is not None
            else {"head": None, "tree": None, "entities": {}}
        )
        entities = dict(index.get("entities") or {})
        entries = dict(index.get("tree_entries") or {})
        for prefix in sorted((manifest.get("shards") or {})):
            shard = self.read_projection_shard(prefix, manifest)
            for entity_key, state in (shard.get("entities") or {}).items():
                if state is None:
                    entities.pop(entity_key, None)
                else:
                    entities[entity_key] = state
            for entity_key, digest in (shard.get("entries") or {}).items():
                if digest is None:
                    entries.pop(entity_key, None)
                else:
                    entries[entity_key] = digest
        return {
            **index,
            "schema_version": SCHEMA_VERSION,
            "head": manifest.get("head"),
            "tree": manifest.get("tree"),
            "last_db_commit_id": manifest.get("last_db_commit_id"),
            "entities": entities,
            "tree_entries": entries,
        }

    def write_index(self, index: dict) -> None:
        _atomic_write_text(self.root / "index.json",
                           json.dumps(index, sort_keys=True, ensure_ascii=False))

    def migrate_v1_to_v2(self) -> dict[str, int | bool]:
        """Explicit per-repository compaction of v1/base-overlay into shards."""

        with self.projection_lock():
            manifest = self.projection_manifest_for_update()
            config = self.read_config()
            if (
                manifest.get("base") is None
                and int(config.get("schema_version") or 0) >= SCHEMA_VERSION
            ):
                persisted = self.read_projection_manifest()
                if persisted is None or persisted.get("head") != manifest.get("head"):
                    self.write_projection_manifest(manifest)
                return {"migrated": False, "entities": 0, "shards": 0}

            index = self.read_index()
            entities = dict(index.get("entities") or {})
            entries = dict(index.get("tree_entries") or {})
            shard_rows: dict[str, dict[str, dict]] = {}
            new_blob_payloads: list[dict] = []
            for entity_key, state in entities.items():
                prefix = self.shard_for_entity(entity_key)
                bucket = shard_rows.setdefault(
                    prefix,
                    {"entities": {}, "entries": {}},
                )
                digest = entries.get(entity_key)
                if not digest:
                    subject_type, _, subject_id = entity_key.partition("/")
                    blob = {
                        "type": "blob",
                        "subject_type": subject_type,
                        "subject_id": subject_id,
                        "state": state,
                    }
                    digest = hash_payload(blob)
                    new_blob_payloads.append(blob)
                bucket["entities"][entity_key] = state
                bucket["entries"][entity_key] = digest

            shard_payloads = [
                {
                    "type": "tree-shard-v2",
                    "prefix": prefix,
                    "entities": bucket["entities"],
                    "entries": bucket["entries"],
                }
                for prefix, bucket in sorted(shard_rows.items())
            ]
            migration_payloads = [*new_blob_payloads, *shard_payloads]
            for start in range(0, len(migration_payloads), _MIGRATION_OBJECT_BATCH):
                self.objects.write_many(
                    migration_payloads[start:start + _MIGRATION_OBJECT_BATCH]
                )
            shards = {
                payload["prefix"]: hash_payload(payload)
                for payload in shard_payloads
            }
            head = self.head_commit()
            head_object = self.objects.read(head) if head else {}
            migrated = {
                "schema_version": SCHEMA_VERSION,
                "generation": int(manifest.get("generation") or 0) + 1,
                "head": head,
                "tree": manifest.get("tree") or head_object.get("tree"),
                "last_db_commit_id": (
                    manifest.get("last_db_commit_id")
                    or head_object.get("db_commit_id")
                ),
                "base": None,
                "shards": shards,
            }
            self.write_projection_manifest(migrated)
            config["schema_version"] = SCHEMA_VERSION
            config["projection_layout"] = "sharded-v2"
            config["entity_shard"] = "sha256-prefix-2"
            _atomic_write_text(
                self.root / "config.json",
                json.dumps(config, indent=2, ensure_ascii=False),
            )
            return {
                "migrated": True,
                "entities": len(entities),
                "shards": len(shards),
            }

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

    def has_projected_db_commit_id(self, commit_id: str) -> bool:
        """O(1) memory anomaly check for a historical outbox intent."""

        cur = self.head_commit()
        guard = 0
        while cur and guard < 1_000_000:
            guard += 1
            try:
                obj = self.objects.read(cur)
            except OSError:
                return False
            if obj.get("db_commit_id") == commit_id:
                return True
            cur = obj.get("parent")
        return False
