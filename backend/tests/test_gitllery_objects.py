from pathlib import Path
import os

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


def test_write_many_packs_many_objects_with_one_fsync(tmp_path: Path, monkeypatch):
    store = ObjectStore(tmp_path / "objects")
    calls = 0
    real_fsync = os.fsync

    def counting_fsync(fd):
        nonlocal calls
        calls += 1
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", counting_fsync)
    payloads = [
        {"type": "blob", "subject_id": str(index), "state": {"n": index}}
        for index in range(25)
    ]

    digests = store.write_many(payloads)

    assert calls == 1
    assert [store.read(digest) for digest in digests] == payloads
    assert len(list((tmp_path / "objects" / "packs").glob("*.pack"))) == 1
    assert len({
        (tmp_path / "objects" / digest[:2] / digest[2:]).stat().st_ino
        for digest in digests
    }) == len(digests)


def test_corrupt_pack_pointer_does_not_damage_siblings(tmp_path: Path):
    store = ObjectStore(tmp_path / "objects")
    payloads = [{"type": "blob", "state": {"n": index}} for index in range(2)]
    first, second = store.write_many(payloads)

    (tmp_path / "objects" / first[:2] / first[2:]).write_bytes(b"corrupt")

    assert store.verify(first) is False
    assert store.verify(second) is True
    assert store.read(second) == payloads[1]
