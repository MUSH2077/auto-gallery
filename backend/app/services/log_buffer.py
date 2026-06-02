"""In-memory ring buffer for accessing recent application logs via API."""

import logging
import traceback
from collections import deque
from datetime import datetime, timezone

MAX_ENTRIES = 2000

_buffer: deque[dict] = deque(maxlen=MAX_ENTRIES)


class RingBufferHandler(logging.Handler):
    """Stores recent log entries in a ring buffer, including tracebacks."""

    def emit(self, record: logging.LogRecord):
        try:
            ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
            msg = record.getMessage()

            # Capture exception traceback if present
            if record.exc_info and record.exc_info[1]:
                exc_text = traceback.format_exception(*record.exc_info)
                msg = msg + "\n" + "".join(exc_text)
            elif record.exc_text:
                msg = msg + "\n" + record.exc_text

            _buffer.append({
                "ts": ts,
                "level": record.levelname,
                "name": record.name,
                "msg": msg,
            })
        except Exception:
            pass


def install():
    """Install the ring buffer handler on the root logger."""
    handler = RingBufferHandler()
    handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)


def get_recent(limit: int = 200, level: str | None = None, name_filter: str | None = None) -> list[dict]:
    """Get recent log entries, optionally filtered. Returns newest-first."""
    entries = list(_buffer)
    if level:
        entries = [e for e in entries if e["level"].upper() == level.upper()]
    if name_filter:
        entries = [e for e in entries if name_filter.lower() in e["name"].lower()]
    return list(reversed(entries))[:limit]


def clear():
    _buffer.clear()
