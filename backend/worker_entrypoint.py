"""Spawn multiple RQ worker subprocesses for parallel job processing.

Usage:
    python worker_entrypoint.py <queue_name> <concurrency>

Example:
    python worker_entrypoint.py downloads 3
    python worker_entrypoint.py imports 2
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
    concurrency = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    cmd = ["rq", "worker", "--url", REDIS_URL, queue]

    procs: list[subprocess.Popen] = []
    running = True

    def shutdown(signum, frame):
        nonlocal running
        running = False
        print(f"\nShutting down {len(procs)} worker(s) on queue '{queue}'...")
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
        proc = subprocess.Popen(cmd)
        procs.append(proc)
        print(f"Started worker {i+1}/{concurrency} on queue '{queue}' (pid={proc.pid})")

    while running:
        dead = [p for p in procs if p.poll() is not None]
        for p in dead:
            print(f"Worker pid={p.pid} exited with code {p.returncode}")
            procs.remove(p)
            if running:
                new_proc = subprocess.Popen(cmd)
                procs.append(new_proc)
                print(f"Restarted worker on queue '{queue}' (pid={new_proc.pid})")
        time.sleep(1)

    for p in procs:
        p.wait(timeout=10)
    print("All workers stopped.")


if __name__ == "__main__":
    main()
