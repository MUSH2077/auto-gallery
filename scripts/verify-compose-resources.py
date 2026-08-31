#!/usr/bin/env python3
"""Validate portable project-local Compose resource invariants."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024

PROTECTED_SERVICE_NAMES = (
    "postgres",
    "redis",
    "meilisearch",
    "migrate",
)

APPLICATION_SERVICE_NAMES = (
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


def verify_resource_limits(services: dict) -> None:
    for name in PROTECTED_SERVICE_NAMES:
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

    for name in APPLICATION_SERVICE_NAMES:
        service = services[name]
        for limit in ("mem_limit", "memswap_limit", "cpus"):
            require(limit not in service, f"{name}: {limit} must be unset")
        require(int(service["pids_limit"]) >= 16, f"{name}: PID limit is too small")
        require(-1000 <= service.get("oom_score_adj", 0) <= 1000, f"{name}: bad OOM score")
        logging = service["logging"]
        require(logging["driver"] == "json-file", f"{name}: bad log driver")
        require(
            logging["options"] == {"max-file": "3", "max-size": "10m"},
            f"{name}: bad log rotation",
        )


def verify_runtime_resource_contract(config: dict) -> None:
    """Exercise the live verifier against inspect data derived from Compose.

    This keeps the resolved base/overlay configuration and the post-deploy
    Docker inspection contract independent: a change to either side must still
    satisfy the other side's actual behavior.
    """

    fixtures: dict[str, dict[str, int | str]] = {}
    services = config["services"]
    for name in (*PROTECTED_SERVICE_NAMES, *APPLICATION_SERVICE_NAMES):
        service = services[name]
        fixtures[name] = {
            "memory": int(service.get("mem_limit") or 0),
            "memory_swap": int(service.get("memswap_limit") or 0),
            "nano_cpus": int(float(service.get("cpus") or 0) * 1_000_000_000),
            "pids": int(service.get("pids_limit") or 0),
            "oom_score": int(service.get("oom_score_adj") or 0),
            "oom_killed": "false",
            "restart_count": 0,
        }

    with tempfile.TemporaryDirectory(prefix="auto-gallery-runtime-contract-") as raw_dir:
        directory = Path(raw_dir)
        fixture_path = directory / "inspect.json"
        inspect_log = directory / "inspect.log"
        fixture_path.write_text(json.dumps(fixtures), encoding="utf-8")
        docker = directory / "docker"
        docker.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys

fixtures = json.load(open(os.environ["RUNTIME_CONTRACT_FIXTURE"], encoding="utf-8"))
args = sys.argv[1:]
if args and args[0] == "compose":
    if "ps" in args:
        service = args[args.index("ps") + 1]
        if "-q" in args:
            service = args[-1]
            if service in fixtures:
                print(service)
                raise SystemExit(0)
            raise SystemExit(1)
        if "{{.State}}" in args:
            print("running")
            raise SystemExit(0)
        if "{{.Health}}" in args:
            print("healthy")
            raise SystemExit(0)
        raise SystemExit(2)
    if "exec" in args:
        raise SystemExit(0)
elif len(args) >= 2 and args[0] == "inspect":
    with open(os.environ["RUNTIME_CONTRACT_LOG"], "a", encoding="utf-8") as log:
        log.write(args[1] + "\\n")
    values = fixtures[args[1]]
    print(
        values["memory"],
        values["memory_swap"],
        values["nano_cpus"],
        values["pids"],
        values["oom_score"],
        values["oom_killed"],
        values["restart_count"],
    )
    raise SystemExit(0)
raise SystemExit(2)
""",
            encoding="utf-8",
        )
        docker.chmod(0o700)
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{directory}:{env['PATH']}",
                "COMPOSE_ENV_FILE": str(directory / "absent.env"),
                "RUNTIME_CONTRACT_FIXTURE": str(fixture_path),
                "RUNTIME_CONTRACT_LOG": str(inspect_log),
                "VERIFY_RESOURCES_ONLY": "1",
                "VERIFY_SCOPE": "full",
            }
        )
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/verify-runtime.sh")],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        inspected = (
            set(inspect_log.read_text(encoding="utf-8").splitlines())
            if inspect_log.exists()
            else set()
        )
    require(
        result.returncode == 0,
        "runtime verifier disagrees with resolved Compose:\n"
        + (result.stderr or result.stdout).strip(),
    )
    require(
        inspected == set(fixtures),
        "runtime verifier did not inspect every Compose service: "
        f"missing={sorted(set(fixtures) - inspected)} extra={sorted(inspected - set(fixtures))}",
    )


def verify_base(config: dict) -> None:
    services = config["services"]
    verify_resource_limits(services)

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
        require(
            services[name]["environment"]["RESOURCE_MEMORY_RESERVE_RATIO"] == "0.15",
            f"{name}: reserve ratio changed",
        )
        require(
            services[name]["environment"]["RESOURCE_MEMORY_RESERVE_MIN_MB"] == "384",
            f"{name}: reserve minimum changed",
        )
        require(
            services[name]["environment"]["RESOURCE_MEMORY_RESERVE_MAX_MB"] == "2560",
            f"{name}: reserve maximum must default to 2560 MiB",
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
    verify_resource_limits(services)
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
    verify_resource_limits(services)
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
        base = compose_config("docker-compose.yaml")
        verify_base(base)
        verify_runtime_resource_contract(base)
        io_override = compose_config(
            "docker-compose.yaml",
            "docker-compose.nas-io.yaml",
            extra_env={"NAS_BLOCK_DEVICE": "/dev/sdb"},
        )
        verify_io_override(io_override)
        verify_runtime_resource_contract(io_override)
        test_root = "/tmp/auto-gallery-compose-test/run"
        test_override = compose_config(
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
                "HOST_RESTORE_STAGING": f"{test_root}/restore-staging",
                "HOST_RESTORE_RECEIPTS": f"{test_root}/restore-receipts",
                "BACKEND_IMAGE": "auto-gallery-backend:candidate-contract",
                "ADMIN_IMAGE": "auto-gallery-admin-web:candidate-contract",
                "BACKEND_PORT": "18818",
                "ADMIN_WEB_PORT": "13080",
            },
        )
        verify_test_override(test_override)
        verify_runtime_resource_contract(test_override)
    except (AssertionError, FileNotFoundError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"compose resource verification failed: {exc}", file=sys.stderr)
        return 1
    print("compose resource contract: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
