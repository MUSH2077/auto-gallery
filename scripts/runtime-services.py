#!/usr/bin/env python3
"""Resolve optional runtime services from the effective Compose environment."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
import sys
from typing import Any


DISCOVERY_REGISTRIES = ("queued", "started", "scheduled", "deferred", "retry")

DISCOVERY_FLAGS = (
    "REMOTE_DISCOVERY_PIXIV_PREVIEW_ENABLED",
    "REMOTE_DISCOVERY_X_ENABLED",
    "REMOTE_DISCOVERY_BILIBILI_ENABLED",
)


def _enabled(value: Any) -> bool:
    if value is None:
        return False
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "y", "t"}:
        return True
    if normalized in {"0", "false", "no", "off", "n", "f"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def discovery_enabled_environment(environment: Mapping[str, Any] | list[str]) -> bool:
    if isinstance(environment, list):
        values: dict[str, Any] = {}
        for item in environment:
            key, separator, value = str(item).partition("=")
            if separator:
                values[key] = value
        environment = values
    if not isinstance(environment, Mapping):
        raise ValueError("invalid container environment")
    if not _enabled(environment.get("REMOTE_DISCOVERY_PRIVATE_MEMBERS_ENABLED")):
        return False
    return any(_enabled(environment.get(flag)) for flag in DISCOVERY_FLAGS)


def discovery_enabled(compose_config: dict[str, Any]) -> bool:
    services = compose_config.get("services") or {}
    return any(
        discovery_enabled_environment(
            (services.get(name) or {}).get("environment") or {}
        )
        for name in ("backend", "scheduler")
    )


def discovery_work_pending(
    payload: Mapping[str, Any],
) -> bool:
    queue_counts = payload.get("queue") or {}
    persisted = int(payload.get("persisted_nonterminal") or 0)
    return persisted > 0 or any(
        int(queue_counts.get(name) or 0) > 0 for name in DISCOVERY_REGISTRIES
    )


def worker_services(compose_config: dict[str, Any]) -> list[str]:
    services = [
        "worker-download",
        "worker-import",
        "worker-operations",
    ]
    if discovery_enabled(compose_config):
        services.append("worker-discovery")
    services.append("scheduler")
    return services


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check-discovery-backlog",
        action="store_true",
        help="read queue and persisted TaskRun counts and report whether work remains",
    )
    parser.add_argument(
        "--check-discovery-environment",
        action="store_true",
        help="read a container environment list/object and report effective discovery enablement",
    )
    args = parser.parse_args()
    try:
        config = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, UnicodeError) as exc:
        print(f"invalid runtime input: {exc}", file=sys.stderr)
        return 2
    if args.check_discovery_environment:
        if not isinstance(config, (dict, list)):
            print("invalid runtime input: expected an environment object/list", file=sys.stderr)
            return 2
    elif not isinstance(config, dict):
        print("invalid runtime input: expected an object", file=sys.stderr)
        return 2
    if args.check_discovery_backlog:
        print("1" if discovery_work_pending(config) else "0")
        return 0
    if args.check_discovery_environment:
        try:
            print("1" if discovery_enabled_environment(config) else "0")
        except ValueError as exc:
            print(f"invalid container environment: {exc}", file=sys.stderr)
            return 2
        return 0
    try:
        services = worker_services(config)
    except ValueError as exc:
        print(f"invalid effective flag: {exc}", file=sys.stderr)
        return 2
    print("\n".join(services))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
