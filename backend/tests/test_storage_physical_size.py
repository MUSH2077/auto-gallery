import os

from app.api.system import _dir_size


def test_directory_size_counts_hardlinked_inode_once(tmp_path):
    original = tmp_path / "original.bin"
    alias = tmp_path / "alias.bin"
    original.write_bytes(b"physical-bytes")
    os.link(original, alias)

    assert _dir_size(str(tmp_path)) == len(b"physical-bytes")
