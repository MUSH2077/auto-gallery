#!/usr/bin/env python3
"""Pause an acceptance container before host PSI reaches guardian limits."""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import time
from pathlib import Path


def avg10(path: str, row: str = "full") -> float:
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{row} "):
            for item in line.split()[1:]:
                key, value = item.split("=", 1)
                if key == "avg10":
                    return float(value)
    raise RuntimeError(f"missing {row} PSI row in {path}")


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, text=True, capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--baseline-file")
    parser.add_argument("--pause-memory-delta", type=float, default=10.0)
    parser.add_argument("--pause-io-delta", type=float, default=20.0)
    parser.add_argument("--resume-memory-delta", type=float, default=5.0)
    parser.add_argument("--resume-io-delta", type=float, default=10.0)
    parser.add_argument("--resume-samples", type=int, default=5)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")

    if args.baseline_file:
        baseline = json.loads(Path(args.baseline_file).read_text(encoding="utf-8"))
        baseline_memory = float(baseline["memory_psi_full_avg10"]["p95"])
        baseline_io = float(baseline["io_psi_full_avg10"]["p95"])
    else:
        # Non-acceptance callers get a local point-in-time baseline. Acceptance
        # always passes the shared five-minute baseline collected by guardian.
        baseline_memory = avg10("/proc/pressure/memory")
        baseline_io = avg10("/proc/pressure/io")
    pause_memory = baseline_memory + args.pause_memory_delta
    pause_io = baseline_io + args.pause_io_delta
    resume_memory = baseline_memory + args.resume_memory_delta
    resume_io = baseline_io + args.resume_io_delta

    labels = docker("inspect", args.container, "--format", "{{json .Config.Labels}}").stdout
    if f'"com.docker.compose.project":"{args.project}"' not in labels or (
        '"com.auto-gallery.environment":"acceptance"' not in labels
    ):
        raise SystemExit("refusing to control a container without exact acceptance labels")

    stable = 0
    while stable < args.resume_samples:
        memory = avg10("/proc/pressure/memory")
        io = avg10("/proc/pressure/io")
        if memory <= resume_memory and io <= resume_io:
            stable += 1
        else:
            stable = 0
        if stable < args.resume_samples:
            time.sleep(1)

    child = subprocess.Popen(command)
    paused = False
    stable = 0

    def terminate(_signum: int, _frame: object) -> None:
        nonlocal paused
        if paused:
            docker("unpause", args.container, check=False)
            paused = False
        child.terminate()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        while child.poll() is None:
            memory = avg10("/proc/pressure/memory")
            io = avg10("/proc/pressure/io")
            if not paused and (memory >= pause_memory or io >= pause_io):
                result = docker("pause", args.container, check=False)
                if result.returncode:
                    if child.poll() is not None:
                        break
                    state = docker(
                        "inspect", args.container, "--format", "{{.State.Paused}}", check=False
                    )
                    if state.returncode or state.stdout.strip() != "true":
                        detail = result.stderr.strip() or "container disappeared during shutdown"
                        raise RuntimeError(f"cannot pause acceptance container: {detail}")
                paused = True
                stable = 0
                print(f"pressure controller paused {args.container}: memory={memory:.2f} io={io:.2f}", flush=True)
            elif paused:
                if memory <= resume_memory and io <= resume_io:
                    stable += 1
                else:
                    stable = 0
                if stable >= args.resume_samples:
                    docker("unpause", args.container)
                    paused = False
                    stable = 0
                    print(f"pressure controller resumed {args.container}: memory={memory:.2f} io={io:.2f}", flush=True)
            time.sleep(1)
    finally:
        if paused:
            docker("unpause", args.container, check=False)
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
