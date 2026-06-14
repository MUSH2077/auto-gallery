import json

from app.jobs.import_runner import _consume_import_file_list


class FakeRedis:
    def __init__(self, value):
        self.value = value
        self.deleted = []

    def get(self, key):
        self.key = key
        return self.value

    def delete(self, key):
        self.deleted.append(key)


def test_consume_import_file_list_reads_and_deletes_snapshot_key():
    redis = FakeRedis(json.dumps(["/downloads/pixiv/a.json", "/downloads/pixiv/b.json"]))

    files = _consume_import_file_list(redis, "job-1")

    assert files == ["/downloads/pixiv/a.json", "/downloads/pixiv/b.json"]
    assert redis.key == "import:job-1:files"
    assert redis.deleted == ["import:job-1:files"]


def test_consume_import_file_list_returns_none_without_snapshot():
    redis = FakeRedis(None)

    assert _consume_import_file_list(redis, "job-1") is None
    assert redis.deleted == []
