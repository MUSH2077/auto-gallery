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
