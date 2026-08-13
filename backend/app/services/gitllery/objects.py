"""Content-addressed object store for .gitllery repositories.

Objects are zlib-compressed canonical JSON, addressed by sha256 of the
uncompressed canonical bytes, stored under objects/{hash[:2]}/{hash[2:]}
(git-style sharding). Pure filesystem + hashing; no database access.
"""
from __future__ import annotations

from collections import OrderedDict
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
        # One projection commonly reads several shards published in the same
        # pack. Keep only the two most recent decoded packs so those reads are
        # O(pack), not O(shards * pack), without allowing cache growth.
        self._pack_cache: OrderedDict[str, dict] = OrderedDict()

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

    def write_many(self, payloads: list[dict]) -> list[str]:
        """Write a commit's object graph as one durable pack.

        Every payload retains its ordinary content hash and object path.  The
        paths contain small atomic pointers to one compressed pack, so legacy
        readers and rebuild code can continue addressing objects by digest
        while a bounded projection performs one data fsync instead of one per
        entity. Existing loose/packed objects are reused without rewriting
        them, and corruption of one pointer cannot damage the other objects in
        its pack.
        """

        digests = [hash_payload(payload) for payload in payloads]
        missing: dict[str, dict] = {
            digest: payload
            for digest, payload in zip(digests, payloads, strict=True)
            if not self.exists(digest)
        }
        if not missing:
            return digests

        pack_payload = {
            "type": "gitllery-pack",
            "version": 1,
            "objects": missing,
        }
        raw = canonical_bytes(pack_payload)
        pack_digest = hashlib.sha256(raw).hexdigest()
        packs_dir = self._dir / "packs"
        packs_dir.mkdir(parents=True, exist_ok=True)
        pack_path = packs_dir / f"{pack_digest}.pack"
        if not pack_path.exists():
            tmp = packs_dir / f".{pack_digest}.{uuid.uuid4().hex}.tmp"
            with open(tmp, "wb") as fh:
                fh.write(zlib.compress(raw))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, pack_path)

        # Publish object names only after the complete pack is durable. Each
        # pointer uses replace, so readers see either no object or a complete
        # pointer. A crash before HEAD moves leaves harmless orphan packs and
        # pointers; once HEAD moves every referenced pointer already exists.
        for digest in missing:
            target = self._path_for(digest)
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            pointer = canonical_bytes(
                {
                    "type": "gitllery-pack-pointer",
                    "version": 1,
                    "pack": pack_digest,
                }
            )
            tmp = target.parent / f".{digest}.{uuid.uuid4().hex}.tmp"
            with open(tmp, "wb") as fh:
                fh.write(zlib.compress(pointer))
                fh.flush()
            os.replace(tmp, target)
        return digests

    def read(self, digest: str) -> dict:
        with open(self._path_for(digest), "rb") as fh:
            payload = json.loads(zlib.decompress(fh.read()).decode("utf-8"))
        if payload.get("type") == "gitllery-pack-pointer":
            pack_digest = payload.get("pack")
            if (
                not isinstance(pack_digest, str)
                or len(pack_digest) != 64
                or any(char not in "0123456789abcdef" for char in pack_digest)
            ):
                raise ValueError(f"object {digest} has an invalid pack pointer")
            payload = self._pack_cache.get(pack_digest)
            if payload is None:
                pack_path = self._dir / "packs" / f"{pack_digest}.pack"
                with open(pack_path, "rb") as fh:
                    payload = json.loads(
                        zlib.decompress(fh.read()).decode("utf-8")
                    )
                self._pack_cache[pack_digest] = payload
                if len(self._pack_cache) > 2:
                    self._pack_cache.popitem(last=False)
            else:
                self._pack_cache.move_to_end(pack_digest)
        if payload.get("type") == "gitllery-pack":
            try:
                return payload["objects"][digest]
            except (KeyError, TypeError) as exc:
                raise ValueError(f"object {digest} is absent from its pack") from exc
        return payload

    def verify(self, digest: str) -> bool:
        try:
            raw = canonical_bytes(self.read(digest))
        except (OSError, ValueError, zlib.error):
            return False
        return hashlib.sha256(raw).hexdigest() == digest
