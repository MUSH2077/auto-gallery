from app.services import ws_tickets


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def setex(self, key, ttl, value):
        self.values[key] = value
        self.ttls[key] = ttl

    def getdel(self, key):
        value = self.values.get(key)
        self.values.pop(key, None)
        return value


def test_ws_ticket_is_short_lived_and_consumed_once(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(ws_tickets, "get_redis", lambda: redis)

    ticket, ttl = ws_tickets.issue_ws_ticket("admin")

    assert ttl == 30
    assert redis.ttls[f"ws:ticket:{ticket}"] == 30
    assert ws_tickets.consume_ws_ticket(ticket) == "admin"
    assert ws_tickets.consume_ws_ticket(ticket) is None


def test_ws_ticket_rejects_missing_or_unknown_ticket(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(ws_tickets, "get_redis", lambda: redis)

    assert ws_tickets.consume_ws_ticket("") is None
    assert ws_tickets.consume_ws_ticket("missing") is None
