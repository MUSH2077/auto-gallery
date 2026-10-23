"""Lightweight Redis bridge for the opt-in host download lifecycle controller.

This module deliberately avoids app configuration, database models.
RQ supplies the registry keys; no registry cleanup or job mutation is performed.
The durable download dispatch outbox and its existing recovery remain authoritative.
"""
from __future__ import annotations

import json
import os
import select
import sys
import time

import redis
from rq import Queue
from rq.registry import StartedJobRegistry, ScheduledJobRegistry, DeferredJobRegistry

QUEUES = tuple(["downloads"] + ["downloads:" + source for source in
    ("pixiv", "danbooru", "iwara", "weibo", "bilibili", "pinterest", "lofter", "x")])
WORK_CHANNEL = "resource:work:download_network"
CONTROLLER_KEY = "worker:download:controller:v1"
DRAIN_KEY = "worker:download:drain:v1"
SUPERVISOR_KEY = "worker:supervisors:v1"
SUPERVISOR_FIELD = "+".join(QUEUES).replace(":", "_")
RENEW = """
if redis.call('get', KEYS[1]) == ARGV[1] then
 return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""
REQUEST_DRAIN = """
if redis.call('get', KEYS[1]) == ARGV[1] then
 redis.call('set', KEYS[2], ARGV[2], 'EX', 15)
 redis.call('publish', 'worker:download:drain:control', 'changed')
 return 1
end
return 0
"""
CANCEL_DRAIN = """
if redis.call('get', KEYS[1]) == ARGV[1] then
 return redis.call('del', KEYS[2])
end
return 0
"""
RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
 return redis.call('del', KEYS[1])
end
return 0
"""


def queue_snapshot(connection):
    pipe = connection.pipeline(transaction=True)
    pipe.time()
    for name in QUEUES:
        queue = Queue(name, connection=connection)
        pipe.llen(queue.key)
        pipe.llen(queue.intermediate_queue_key)
        pipe.zcard(StartedJobRegistry(name, connection=connection).key)
        pipe.zcard(DeferredJobRegistry(name, connection=connection).key)
        pipe.zrange(ScheduledJobRegistry(name, connection=connection).key, 0, 0, withscores=True)
    pipe.hget(SUPERVISOR_KEY, SUPERVISOR_FIELD)
    values = pipe.execute()
    clock = values[0][0] + values[0][1] / 1_000_000
    queues = {}
    deadlines = []
    for index, name in enumerate(QUEUES):
        queued, intermediate, started, deferred, scheduled = values[1 + index * 5:6 + index * 5]
        due = float(scheduled[0][1]) if scheduled else None
        if due is not None:
            deadlines.append(due - clock)
        queues[name] = dict(queued=queued, intermediate=intermediate,
                            started=started, deferred=deferred, next_due_in_s=None if due is None else due-clock)
    raw = values[-1]
    supervisor = json.loads(raw) if raw else {}
    return dict(queues=queues, runnable=sum(q["queued"] + q["intermediate"] for q in queues.values()),
                inflight=sum(q["started"] for q in queues.values()),
                deferred=sum(q["deferred"] for q in queues.values()),
                next_due_in_s=min(deadlines) if deadlines else None, supervisor=supervisor)


class Bridge:
    def __init__(self, connection, token):
        self.connection = connection
        self.token = token
        self.owned = False
        self.drain_token = None

    def command(self, value):
        action = value["action"]
        if action == "acquire":
            self.owned = bool(self.connection.set(CONTROLLER_KEY, self.token, nx=True, ex=30)
                              or self.connection.eval(RENEW, 1, CONTROLLER_KEY, self.token, 30))
            return {"owned": self.owned}
        if not self.owned or not self.connection.eval(RENEW, 1, CONTROLLER_KEY, self.token, 30):
            raise RuntimeError("download lifecycle controller lease lost")
        if action == "drain":
            self.drain_token = value["drain_token"]
            if not self.drain_token.startswith(self.token + ":"):
                raise ValueError("invalid drain owner")
            if not self.connection.eval(REQUEST_DRAIN, 2, CONTROLLER_KEY, DRAIN_KEY, self.token, self.drain_token):
                raise RuntimeError("download lifecycle controller lease lost")
        elif action in ("cancel", "release"):
            self.connection.eval(CANCEL_DRAIN, 2, CONTROLLER_KEY, DRAIN_KEY, self.token)
            self.connection.publish("worker:download:drain:control", "changed")
        elif action != "snapshot":
            raise ValueError("unknown lifecycle action")
        result = queue_snapshot(self.connection)
        if action == "release":
            self.connection.eval(RELEASE, 1, CONTROLLER_KEY, self.token)
            self.owned = False
        return result


def main():
    # Credentials stay inside the existing application container environment.
    connection = redis.Redis.from_url(os.environ["REDIS_URL"], socket_timeout=3,
                                     socket_connect_timeout=3, decode_responses=True)
    bridge = Bridge(connection, sys.argv[1])
    subscriber = connection.pubsub(ignore_subscribe_messages=True)
    subscriber.subscribe(WORK_CHANNEL)
    def emit(value):
        print(json.dumps(value, separators=(",", ":")), flush=True)
    try:
        while True:
            readable, _, _ = select.select([sys.stdin], [], [], 0.05)
            if readable:
                line = sys.stdin.readline()
                if not line:
                    break
                request = json.loads(line)
                try:
                    emit({"id": request["id"], "value": bridge.command(request)})
                except Exception as exc:
                    # No URL, exception detail or job payload goes to the host.
                    emit({"id": request["id"], "error": type(exc).__name__})
            if subscriber.get_message(timeout=0):
                emit({"event": "work"})
    finally:
        subscriber.close()
        if bridge.owned:
            try:
                connection.eval(RELEASE, 1, DRAIN_KEY, bridge.drain_token or "")
                # The host owns the controller lease across bridge reconnects.
                # A delayed old bridge must not release its replacement's lease.
                # Normal host exit sends release; an unclean host exit expires.
            except redis.RedisError:
                pass  # Expiry restores any still-running supervisor.
        connection.close()


if __name__ == "__main__":
    main()
