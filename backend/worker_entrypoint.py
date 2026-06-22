"""Spawn multiple RQ worker subprocesses.

Usage (RQ workers):
    python worker_entrypoint.py <queue_name> <concurrency> [--with-scheduler]
"""

import os
import signal
import subprocess
import sys
import time

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <queue_name> [concurrency]", file=sys.stderr)
        sys.exit(1)

    queue = sys.argv[1]
    queues = queue.split(",") if "," in queue else [queue]
    concurrency = int(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else 1
    with_scheduler = "--with-scheduler" in sys.argv[2:]

    procs: list[subprocess.Popen] = []
    running = True

    def shutdown(signum, frame):
        nonlocal running
        running = False
        print(f"\nShutting down {len(procs)} worker(s) on queues {queues}...")
        for p in procs:
            p.terminate()
        # Grace period then force-kill
        time.sleep(5)
        for p in procs:
            if p.poll() is None:
                p.kill()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    for i in range(concurrency):
        cmd = ["rq", "worker", "--url", REDIS_URL]
        if with_scheduler and i == 0:
            cmd.append("--with-scheduler")
        cmd.extend(queues)
        proc = subprocess.Popen(cmd)
        procs.append(proc)
        scheduler_note = " with scheduler" if with_scheduler and i == 0 else ""
        print(f"Started worker {i+1}/{concurrency} on queues {queues}{scheduler_note} (pid={proc.pid})")

    while running:
        dead = [p for p in procs if p.poll() is not None]
        for p in dead:
            print(f"Worker pid={p.pid} exited with code {p.returncode}")
            procs.remove(p)
            if running:
                idx = len(procs)
                restart_cmd = ["rq", "worker", "--url", REDIS_URL]
                if with_scheduler and not any("--with-scheduler" in getattr(proc, "args", []) for proc in procs):
                    restart_cmd.append("--with-scheduler")
                restart_cmd.extend(queues)
                new_proc = subprocess.Popen(restart_cmd)
                procs.append(new_proc)
                print(f"Restarted worker on queues {queues} (pid={new_proc.pid})")
        time.sleep(1)

    for p in procs:
        p.wait(timeout=10)
    print("All workers stopped.")

if __name__ == "__main__":
    main()
