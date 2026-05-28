"""In-memory ring buffer for accessing recent application logs via API."""

import logging
from collections import deque
from datetime import datetime, timezone

MAX_ENTRIES = 2000

_buffer: deque[dict] = deque(maxlen=MAX_ENTRIES)


class RingBufferHandler(logging.Handler):
    """Logging handler that stores recent log entries in a ring buffer."""

    def emit(self, record: logging.LogRecord):
        try:
            ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
            _buffer.append({
                "ts": ts,
                "level": record.levelname,
                "name": record.name,
                "msg": self.format(record),
            })
        except Exception:
            pass


def install():
    """Install the ring buffer handler on the root logger."""
    handler = RingBufferHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)


def get_recent(limit: int = 200, level: str | None = None, name_filter: str | None = None) -> list[dict]:
    """Get recent log entries, optionally filtered. Returns newest-first."""
    entries = list(_buffer)
    if level:
        entries = [e for e in entries if e["level"].upper() == level.upper()]
    if name_filter:
        entries = [e for e in entries if name_filter.lower() in e["name"].lower()]
    # reversed(entries) puts newest first; [:limit] keeps the most recent N
    return list(reversed(entries))[:limit]


def clear():
    _buffer.clear()
