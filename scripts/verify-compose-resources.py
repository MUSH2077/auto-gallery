#!/usr/bin/env python3
"""Validate portable project-local Compose resource invariants."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024

SERVICE_NAMES = (
    "postgres",
    "redis",
    "meilisearch",
    "backend",
    "worker-download",
    "worker-import",
    "worker-operations",
    "scheduler",
    "admin-web",
)


def compose_config(
    *files: str,
    extra_env: dict[str, str] | None = None,
    profiles: tuple[str, ...] = (),
) -> dict:
    env = os.environ.copy()
    env.update(
        {
            "POSTGRES_PASSWORD": "compose-verification",
            "REDIS_PASSWORD": "compose-verification",
            "MEILI_MASTER_KEY": "compose-verification",
            "SECRET_KEY": "compose-verification-secret-key-32-bytes",
            "ADMIN_PASSWORD": "compose-verification",
            "RESOURCE_GOVERNANCE_MODE": "enforce",
            "RESOURCE_GOVERNANCE_ENFORCED_PROFILES": "download_network,import_db,image_derive,video_derive,search_index,maintenance",
            "RESOURCE_MEMORY_RESERVE_MODE": "auto",
        }
    )
    if extra_env:
        env.update(extra_env)

    command = ["docker", "compose"]
    for filename in files:
        command.extend(("-f", filename))
    for profile in profiles:
        command.extend(("--profile", profile))
    command.extend(("config", "--format", "json"))
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return json.loads(result.stdout)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def verify_base(config: dict) -> None:
    services = config["services"]
    for name in SERVICE_NAMES:
        service = services[name]
        memory = int(service["mem_limit"])
        require(memory >= 64 * MIB, f"{name}: mem_limit is too small")
        require(
            int(service["memswap_limit"]) == memory,
            f"{name}: container swap must be disabled",
        )
        require(float(service["cpus"]) > 0, f"{name}: CPU limit must be positive")
        require(int(service["pids_limit"]) >= 16, f"{name}: PID limit is too small")
        require(-1000 <= service.get("oom_score_adj", 0) <= 1000, f"{name}: bad OOM score")
        logging = service["logging"]
        require(logging["driver"] == "json-file", f"{name}: bad log driver")
        require(
            logging["options"] == {"max-file": "3", "max-size": "10m"},
            f"{name}: bad log rotation",
        )

    # The heaviest unit that can execute in each worker must fit its cgroup.
    require(int(services["worker-download"]["mem_limit"]) >= 128 * MIB, "download profile cannot fit")
    require(int(services["worker-import"]["mem_limit"]) >= 384 * MIB, "video profile cannot fit import worker")
    require(int(services["worker-operations"]["mem_limit"]) >= 384 * MIB, "video profile cannot fit operations worker")

    for name in ("worker-download", "worker-import", "worker-operations", "scheduler"):
        service = services[name]
        require(service.get("init") is True, f"{name}: init must be enabled")
        require(service.get("stop_grace_period") == "1m0s", f"{name}: bad stop grace period")
        command = service["command"]
        if name == "scheduler":
            scheduler_command = " ".join(command)
            require(
                "exec nice -n 10 ionice -c 2 -n 7" in scheduler_command,
                "scheduler: priority wrapper missing",
            )
            require(
                "worker_entrypoint.py scheduled 1 --with-scheduler" in scheduler_command,
                "scheduler must use the supervised worker entrypoint",
            )
        else:
            require(
                command[:8] == ["nice", "-n", "10", "ionice", "-c", "2", "-n", "7"],
                f"{name}: priority wrapper missing",
            )

    download = services["worker-download"]
    require(download["environment"]["DOWNLOAD_CONCURRENCY_CAP"] == "1", "download cap must default to one")
    require(
        download["environment"]["DOWNLOAD_STAGING_ENABLED"] == "1",
        "download staging must default to enabled",
    )
    require(
        services["backend"]["environment"]["DOWNLOAD_CONCURRENCY_CAP"] == "1",
        "backend health must report the same download cap",
    )
    for name in ("backend", "worker-download", "scheduler"):
        require(
            services[name]["environment"]["DOWNLOAD_QUEUE_MAX_PENDING"] == "100",
            f"{name}: download waiting-job ceiling must default to 100",
        )
    for name in (
        "backend",
        "worker-download",
        "worker-import",
        "worker-operations",
        "scheduler",
    ):
        require(
            services[name]["environment"]["RESOURCE_GOVERNANCE_MODE"] == "enforce",
            f"{name}: adaptive controller must start in enforce mode",
        )
        require(
            "git_projection" not in services[name]["environment"]["RESOURCE_GOVERNANCE_ENFORCED_PROFILES"],
            f"{name}: Gitllery must stay outside the enforced profile list",
        )
        require(
            services[name]["environment"]["RESOURCE_MEMORY_RESERVE_MODE"] == "auto",
            f"{name}: device-relative reserve must default to auto",
        )
    require(download["command"][-2:] == ["1", "--with-scheduler"], "download CLI fallback must be one")
    require(
        "--with-scheduler" in download["command"],
        "download worker must promote queue-scoped delayed retries",
    )
    require(
        "--with-scheduler" in services["worker-import"]["command"],
        "import worker must promote queue-scoped delayed retries",
    )
    require(
        "--with-scheduler" in services["worker-operations"]["command"],
        "operations worker must promote queue-scoped delayed retries",
    )
    for name in ("worker-download", "worker-import", "worker-operations", "scheduler"):
        probe = " ".join(services[name]["healthcheck"]["test"])
        require("scripts/check_worker_health.py" in probe, f"{name}: supervisor-aware health probe missing")

    backend_dependencies = services["backend"].get("depends_on", {})
    require("meilisearch" not in backend_dependencies, "backend must not hard-depend on Meilisearch")
    require(
        backend_dependencies.get("migrate", {}).get("condition") == "service_completed_successfully",
        "backend must wait for migrations",
    )
    require("alembic" not in " ".join(services["backend"]["command"]), "backend still runs migrations")

    redis = services["redis"]
    require("noeviction" in " ".join(redis["command"]), "Redis noeviction policy changed")
    redis_probe = " ".join(redis["healthcheck"]["test"])
    require(" SET " in redis_probe and " DEL " in redis_probe, "Redis write probe missing")
    require(redis["healthcheck"]["interval"] == "30s", "Redis write probe is too frequent")
    require(redis["healthcheck"]["retries"] == 3, "Redis health retry budget changed")
    require(redis["healthcheck"]["start_interval"] == "5s", "Redis startup probe is too slow")

    postgres_command = " ".join(services["postgres"]["command"])
    for setting in (
        "shared_buffers=192MB",
        "work_mem=2MB",
        "maintenance_work_mem=64MB",
        "autovacuum_work_mem=32MB",
        "effective_cache_size=512MB",
        "max_connections=40",
        "max_worker_processes=4",
        "max_parallel_workers=2",
        "max_parallel_workers_per_gather=1",
        "idle_in_transaction_session_timeout=60s",
        "temp_file_limit=256MB",
        "shared_preload_libraries=pg_stat_statements",
        "pg_stat_statements.max=1000",
        "pg_stat_statements.track=top",
        "track_io_timing=on",
    ):
        require(setting in postgres_command, f"PostgreSQL setting missing: {setting}")


def verify_io_override(config: dict) -> None:
    services = config["services"]
    expected = {
        "meilisearch": (20, 10),
        "worker-download": (20, 10),
        "worker-operations": (20, 10),
        "worker-import": (30, 15),
    }
    for name, (read_mib, write_mib) in expected.items():
        limits = services[name]["blkio_config"]
        read = limits["device_read_bps"][0]
        write = limits["device_write_bps"][0]
        require(read["Path"] == "/dev/sdb", f"{name}: bad read device")
        require(write["Path"] == "/dev/sdb", f"{name}: bad write device")
        require(int(read["Rate"]) == read_mib * MIB, f"{name}: bad read rate")
        require(int(write["Rate"]) == write_mib * MIB, f"{name}: bad write rate")


def verify_test_override(config: dict) -> None:
    services = config["services"]
    require(config["networks"]["default"]["internal"] is True, "test network must be internal")
    for name, service in services.items():
        labels = service.get("labels") or {}
        require(
            labels.get("com.auto-gallery.environment") == "acceptance",
            f"{name}: acceptance label missing",
        )
        for volume in service.get("volumes") or []:
            source = volume.get("source") if isinstance(volume, dict) else str(volume).split(":", 1)[0]
            if source and str(source).startswith("/"):
                require(
                    str(source).startswith("/tmp/auto-gallery-compose-test/")
                    or (
                        name == "pressure-memory"
                        and Path(str(source)).resolve() == ROOT
                        and bool(volume.get("read_only"))
                    ),
                    f"{name}: test bind escaped isolated root: {source}",
                )
    for name in ("backend", "admin-web"):
        for port in services[name].get("ports") or []:
            require(port.get("host_ip") == "127.0.0.1", f"{name}: test port is not loopback-only")
    pressure = services["pressure-memory"]
    require(int(pressure["mem_limit"]) == 512 * MIB, "memory pressure limit changed")
    require(
        int(pressure["memswap_limit"]) == 512 * MIB,
        "memory pressure swap must be disabled",
    )


def main() -> int:
    try:
        verify_base(compose_config("docker-compose.yaml"))
        verify_io_override(
            compose_config(
                "docker-compose.yaml",
                "docker-compose.nas-io.yaml",
                extra_env={"NAS_BLOCK_DEVICE": "/dev/sdb"},
            )
        )
        test_root = "/tmp/auto-gallery-compose-test/run"
        verify_test_override(
            compose_config(
                "docker-compose.yaml",
                "docker-compose.test.yaml",
                profiles=("load",),
                extra_env={
                    "TEST_RUN_ID": "contract",
                    "TEST_ROOT": test_root,
                    "TEST_FIXTURES": f"{test_root}/fixtures",
                    "HOST_POSTGRES": f"{test_root}/postgres",
                    "HOST_REDIS": f"{test_root}/redis",
                    "HOST_MEILISEARCH": f"{test_root}/meilisearch",
                    "HOST_DOWNLOADS": f"{test_root}/downloads",
                    "HOST_LIBRARY": f"{test_root}/library",
                    "HOST_CONFIG_GALLERYDL": f"{test_root}/gallery-dl",
                    "HOST_CONFIG_APP": f"{test_root}/app-config",
                    "BACKEND_IMAGE": "auto-gallery-backend:candidate-contract",
                    "ADMIN_IMAGE": "auto-gallery-admin-web:candidate-contract",
                    "BACKEND_PORT": "18818",
                    "ADMIN_WEB_PORT": "13080",
                },
            )
        )
    except (AssertionError, FileNotFoundError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"compose resource verification failed: {exc}", file=sys.stderr)
        return 1
    print("compose resource contract: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
