from app.services import ws_tickets
from types import SimpleNamespace


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

    ticket, ttl = ws_tickets.issue_ws_ticket(SimpleNamespace(id=7, credential_version=3, username="admin"))

    assert ttl == 30
    assert redis.ttls[f"ws:ticket:{ticket}"] == 30
    assert ws_tickets.consume_ws_ticket(ticket) == ws_tickets.BearerTicket(7, 3)
    assert ws_tickets.consume_ws_ticket(ticket) is None


def test_ws_ticket_rejects_missing_or_unknown_ticket(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(ws_tickets, "get_redis", lambda: redis)

    assert ws_tickets.consume_ws_ticket("") is None
    assert ws_tickets.consume_ws_ticket("missing") is None


def test_browser_ticket_carries_session_and_is_consumed_once(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(ws_tickets, "get_redis", lambda: redis)
    ticket, ttl = ws_tickets.issue_ws_ticket(SimpleNamespace(username="alice"), "opaque-session")
    assert ttl == 30
    assert ws_tickets.consume_ws_ticket(ticket) == ws_tickets.BrowserTicket("alice", "opaque-session")
    assert ws_tickets.consume_ws_ticket(ticket) is None


def test_username_only_tickets_require_a_new_ticket(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(ws_tickets, "get_redis", lambda: redis)
    username = '{"username": "alice", "session_token": "not-a-browser-ticket"}'
    redis.setex("ws:ticket:legacy", 30, username.encode())
    assert ws_tickets.consume_ws_ticket("legacy") is None


def test_malformed_browser_ticket_is_rejected(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(ws_tickets, "get_redis", lambda: redis)
    redis.setex("ws:ticket:broken", 30, b"\x00ag-browser-v1:{broken")
    assert ws_tickets.consume_ws_ticket("broken") is None
