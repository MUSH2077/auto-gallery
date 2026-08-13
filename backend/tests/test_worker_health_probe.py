import json

from scripts.check_worker_health import evaluate_worker_health


class _Redis:
    def __init__(self, payloads, hash_payloads=None):
        self.payloads = payloads
        self.hash_payloads = hash_payloads or {}

    def hgetall(self, _key):
        return self.hash_payloads

    def scan_iter(self, **_kwargs):
        return self.payloads.keys()

    def get(self, key):
        return self.payloads[key]


def _heartbeat(**overrides):
    value = {"state": "running", "worker_count": 1}
    value.update(overrides)
    return json.dumps(value).encode()


def test_worker_health_requires_a_running_child():
    redis = _Redis({b"worker:supervisor:nas:downloads": _heartbeat()})

    assert evaluate_worker_health(redis, hostname="nas") == (True, "ok")


def test_worker_health_rejects_circuit_and_missing_heartbeat():
    circuit = _Redis(
        {
            b"worker:supervisor:nas:downloads": _heartbeat(
                state="circuit_open",
                worker_count=0,
            )
        }
    )

    healthy, reason = evaluate_worker_health(circuit, hostname="nas")
    assert healthy is False
    assert reason == "state=circuit_open workers=0"
    assert evaluate_worker_health(_Redis({}), hostname="nas") == (
        False,
        "worker supervisor heartbeat is missing",
    )


def test_worker_health_requires_every_supervised_child():
    payload = _heartbeat(
        hostname="nas",
        expected_worker_count=2,
        worker_count=1,
    )
    redis = _Redis({}, {b"imports+maintenance": payload})

    healthy, reason = evaluate_worker_health(redis, hostname="nas")

    assert healthy is False
    assert reason == "state=running workers=1/2"
