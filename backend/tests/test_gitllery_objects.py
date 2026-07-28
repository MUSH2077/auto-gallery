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
