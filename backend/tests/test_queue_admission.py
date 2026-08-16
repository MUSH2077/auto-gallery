from types import SimpleNamespace

import pytest


class FakeRedis:
    def __init__(self, *, used=10, maximum=100, writable=True):
        self.used = used
        self.maximum = maximum
        self.writable = writable
        self.values = {}
        self.rejections = 0

    def info(self, section):
        assert section == "memory"
        return {"used_memory": self.used, "maxmemory": self.maximum}

    def set(self, key, value, *, ex):
        if not self.writable:
            raise RuntimeError("OOM command not allowed")
        self.values[key] = (value, ex)
        return True

    def delete(self, key):
        return 1 if self.values.pop(key, None) is not None else 0

    def incr(self, _key):
        self.rejections += 1
        return self.rejections


def test_capacity_gate_probes_write_below_stop_threshold():
    from app.services.queue_admission import ensure_redis_enqueue_capacity

    redis = FakeRedis(used=89, maximum=100)
    sample = ensure_redis_enqueue_capacity(redis)

    assert sample["usage_ratio"] == 0.89
    assert redis.values == {}
    assert redis.rejections == 0


def test_capacity_gate_rejects_at_ninety_percent_and_counts_it():
    from app.services.queue_admission import (
        QueueAdmissionError,
        ensure_redis_enqueue_capacity,
    )

    redis = FakeRedis(used=90, maximum=100)
    with pytest.raises(QueueAdmissionError) as caught:
        ensure_redis_enqueue_capacity(redis)

    assert caught.value.code == "redis_capacity"
    assert caught.value.payload()["usage_ratio"] == 0.9
    assert redis.rejections == 1


def test_capacity_gate_converts_write_probe_failure():
    from app.services.queue_admission import (
        QueueAdmissionError,
        ensure_redis_enqueue_capacity,
    )

    redis = FakeRedis(writable=False)
    with pytest.raises(QueueAdmissionError) as caught:
        ensure_redis_enqueue_capacity(redis)

    assert caught.value.code == "redis_unwritable"
    assert caught.value.payload()["error_type"] == "RuntimeError"
    assert redis.rejections == 1


def test_checked_enqueue_and_enqueue_in_share_the_gate():
    from app.services.queue_admission import checked_enqueue, checked_enqueue_in

    redis = FakeRedis()
    calls = []
    queue = SimpleNamespace(
        name="operations",
        connection=redis,
        enqueue=lambda *args, **kwargs: calls.append(("now", args, kwargs)) or "now-job",
        enqueue_in=lambda delay, *args, **kwargs: calls.append((delay, args, kwargs)) or "later-job",
    )

    assert checked_enqueue(queue, "some.func", 1, job_timeout=30) == "now-job"
    assert checked_enqueue_in(queue, 15, "some.func", 2, job_timeout=30) == "later-job"
    assert calls == [
        ("now", ("some.func", 1), {"job_timeout": 30}),
        (15, ("some.func", 2), {"job_timeout": 30}),
    ]


def test_checked_enqueue_propagates_structured_write_failure():
    from app.services.queue_admission import QueueAdmissionError, checked_enqueue

    redis = FakeRedis()

    def reject(*_args, **_kwargs):
        raise TimeoutError("response unavailable")

    queue = SimpleNamespace(
        name="imports",
        connection=redis,
        enqueue=reject,
    )
    with pytest.raises(QueueAdmissionError) as caught:
        checked_enqueue(queue, "some.func")

    assert caught.value.code == "redis_unwritable"
    assert caught.value.payload()["queue"] == "imports"
    assert caught.value.payload()["error_type"] == "TimeoutError"
    assert redis.rejections == 1


def test_checked_enqueue_does_not_mislabel_application_errors():
    from app.services.queue_admission import checked_enqueue

    redis = FakeRedis()

    def invalid_job(*_args, **_kwargs):
        raise ValueError("function cannot be serialized")

    queue = SimpleNamespace(
        name="operations",
        connection=redis,
        enqueue=invalid_job,
    )
    with pytest.raises(ValueError, match="cannot be serialized"):
        checked_enqueue(queue, object())

    assert redis.rejections == 0
