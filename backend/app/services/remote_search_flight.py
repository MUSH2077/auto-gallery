"""Crash-durable resource fence for remote Meili work on the shared lock volume.

The marker is fsynced before sending HTTP, and removed only after a terminal
receipt is committed. Redis is a derived memory-accounting cache, never the
source of truth. A separate short flock serializes marker/cache updates.
"""
from contextlib import contextmanager
import fcntl
import json
import os

REMOTE_RESERVATION_KEY = "lock:resource-budget:remote-search"


def _path():
    from app.services.heavy_io import _local_lock_path
    return _local_lock_path().with_name("remote-search-flight.json")


@contextmanager
def _guard():
    path = _path().with_suffix(".guard")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o660)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read():
    try:
        return json.loads(_path().read_text())
    except FileNotFoundError:
        return None


def read_marker():
    with _guard():
        return _read()


def _sync_directory():
    fd = os.open(_path().parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_marker(receipt_id: str, workload: str = "search_index"):
    from app.services.resource_pressure import RESOURCE_PROFILES, workload_profile_name
    profile = workload_profile_name(workload)
    payload = {"owner": str(receipt_id), "workload": workload, "profile": profile,
               "reserved_bytes": RESOURCE_PROFILES[profile].memory_reservation_bytes}
    with _guard():
        existing = _read()
        if existing:
            if existing["owner"] != str(receipt_id):
                raise RuntimeError("Another remote search flight is unresolved")
            return
        temp = _path().with_suffix(".tmp")
        with temp.open("w") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, _path())
        _sync_directory()
        from app.services.heavy_io import _get_lease_redis
        _get_lease_redis().set(REMOTE_RESERVATION_KEY, json.dumps(payload))


def clear_marker(receipt_id: str):
    from app.services.heavy_io import _get_lease_redis
    with _guard():
        marker = _read()
        if marker and marker["owner"] == str(receipt_id):
            _path().unlink()
            _sync_directory()
            # Stale accounting is safe if Redis is temporarily unavailable;
            # every admission reconciles it before evaluating the RAM sum.
            try:
                _get_lease_redis().delete(REMOTE_RESERVATION_KEY)
            except Exception:
                pass


def reconcile_reservation(redis_client):
    """Restore accounting after Redis restart; never add an expiry."""
    with _guard():
        marker = _read()
        if marker:
            redis_client.set(REMOTE_RESERVATION_KEY, json.dumps(marker))
        else:
            redis_client.delete(REMOTE_RESERVATION_KEY)
        return marker


def record_task(receipt_id: str, task_uid: int | None):
    """Persist an accepted identity before the SQL response checkpoint."""
    with _guard():
        marker = _read()
        if not marker or marker["owner"] != str(receipt_id):
            raise RuntimeError("Remote search marker ownership lost")
        marker["task_uid"] = task_uid
        temp = _path().with_suffix(".tmp")
        with temp.open("w") as stream:
            json.dump(marker, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, _path())
        _sync_directory()


_RESTORE_AND_FENCE_SCRIPT = """
if ARGV[5] ~= "" then
  redis.call("set", ARGV[6], ARGV[5])
  local remote = cjson.decode(ARGV[5])
  if remote["profile"] == "maintenance"
    or KEYS[1] == "lock:resource-budget:background"
    or KEYS[1] == "lock:heavy-io" then
    return -4
  end
else
  redis.call("del", ARGV[6])
end
"""


def reserve_memory(redis_client, script, *args):
    """Restore durable remote accounting and grant RAM in one Redis execution.

    Redis may restart immediately before EVAL. The marker payload travels in
    that same atomic command, never in a preceding SET that restart can erase.
    The filesystem guard orders this grant against marker transitions.
    """
    with _guard():
        marker = _read()
        return redis_client.eval(_RESTORE_AND_FENCE_SCRIPT + script, *args,
                                 json.dumps(marker) if marker else "", REMOTE_RESERVATION_KEY)
