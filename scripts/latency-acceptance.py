#!/usr/bin/env python3
"""Build a separate, bounded NAS fixture and compare immutable source snapshots.

This never connects to production services. PostgreSQL/Redis/Meili run on an
internal network; all writable mounts live below a marked acceptance root.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / ".superpowers" / "latency-acceptance"
IMAGE = "auto-gallery-backend:candidate-e7a11de3b705168ce6871c8c10fbba9045d5f71b18282aab3e1b3ebdefbeda2f"
NETWORK = "ag-latency-bench"
LABEL = "codex.task=latency-acceptance"
SERVICES = {name: f"ag-latency-bench-{name}" for name in ("postgres", "redis", "meili", "runner")}


def run(args, *, log=None, check=True):
    if log:
        with Path(log).open("a") as output:
            completed = subprocess.run(args, stdout=output, stderr=subprocess.STDOUT)
    else:
        completed = subprocess.run(args, capture_output=True, text=True)
    if check and completed.returncode:
        detail = str(log) if log else completed.stderr[-1500:]
        raise RuntimeError(f"Command failed ({completed.returncode}): {args[0]} — {detail}")
    return completed


def source(variant):
    return f"/workspace/.superpowers/latency-acceptance/source-{variant}/backend"


def execute(variant, command, *, database=None, log=None):
    env = {
        "PYTHONPATH": source(variant),
        "DATABASE_URL": f"postgresql+asyncpg://autogallery:latency-bench-db@latency-postgres:5432/{database or 'latency_' + variant}",
        "REDIS_URL": f"redis://latency-redis:6379/{1 if variant == 'baseline' else 2}",
        "MEILI_INDEX_PREFIX": f"latency_{variant}_",
        "DOWNLOAD_ROOT": f"/latency-data/{variant}/downloads",
        "LIBRARY_ROOT": f"/latency-data/{variant}/library",
        "APP_CONFIG_ROOT": f"/latency-data/{variant}/app-config",
        "LATENCY_BROWSE_URL": f"http://latency-api-{variant}:8000",
    }
    args = ["docker", "exec", "-w", source(variant)]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    return run(args + [SERVICES["runner"], *command], log=log)


def snapshot(revision, variant):
    destination = ROOT / f"source-{variant}"
    if destination.exists():
        raise RuntimeError(f"Source snapshot already exists: {destination}")
    destination.mkdir()
    archive = ROOT / f"{variant}.tar"
    run(["git", "-C", str(PROJECT), "archive", "--format=tar", "--output", str(archive), revision, "backend"])
    run(["tar", "-xf", str(archive), "-C", str(destination)])
    return run(["git", "-C", str(PROJECT), "rev-parse", revision]).stdout.strip()


def setup(candidate):
    ROOT.mkdir(parents=True, exist_ok=True)
    if (ROOT / "state.json").exists():
        raise RuntimeError("Acceptance environment already exists; preserve its evidence")
    for variant in ("baseline", "candidate"):
        for folder in ("downloads", "library", "app-config"):
            (ROOT / "data" / variant / folder).mkdir(parents=True, exist_ok=True)
    for folder in ("postgres", "redis", "meili", "reports"):
        (ROOT / folder).mkdir(exist_ok=True)
    (ROOT / "data" / ".latency-acceptance").write_text("isolated-nas-v1\n")
    revisions = {"baseline": snapshot("eedb6c0", "baseline"), "candidate": snapshot(candidate, "candidate")}
    run(["docker", "network", "create", "--internal", "--label", LABEL, NETWORK])
    common = ["docker", "run", "-d", "--network", NETWORK, "--label", LABEL, "--pids-limit", "128"]
    run(common + ["--name", SERVICES["postgres"], "--network-alias", "latency-postgres", "--cpus", "0.45", "--memory", "768m", "--memory-swap", "768m",
                  "-e", "POSTGRES_USER=autogallery", "-e", "POSTGRES_PASSWORD=latency-bench-db", "-e", "POSTGRES_DB=latency_template",
                  "-v", f"{ROOT / 'postgres'}:/var/lib/postgresql/data", "postgres:16-alpine"])
    run(["docker", "run", "--rm", "--network", "none", "--user", "0:0", "--memory", "64m", "--label", LABEL,
         "-v", f"{ROOT / 'redis'}:/data", "--entrypoint", "chown", "redis:7-alpine", "redis:redis", "/data"])
    # NAS inherited ACLs can deny the image's dropped redis UID even after
    # chown. This isolated service writes only its own marked bind directory.
    run(common + ["--name", SERVICES["redis"], "--network-alias", "latency-redis", "--cpus", "0.15", "--memory", "128m", "--memory-swap", "128m",
                  "--user", "0:0", "--entrypoint", "redis-server",
                  "-v", f"{ROOT / 'redis'}:/data", "redis:7-alpine", "--appendonly", "yes", "--save", ""])
    run(common + ["--name", SERVICES["meili"], "--network-alias", "latency-meili", "--cpus", "0.4", "--memory", "1g", "--memory-swap", "1g",
                  "-e", "MEILI_ENV=development", "-e", "MEILI_NO_ANALYTICS=true", "-e", "MEILI_MASTER_KEY=latency-bench-search-key",
                  "-e", "MEILI_MAX_INDEXING_MEMORY=320Mb", "-e", "MEILI_MAX_INDEXING_THREADS=1", "-v", f"{ROOT / 'meili'}:/meili_data", "getmeili/meilisearch:v1.12"])
    run(common + ["--name", SERVICES["runner"], "--cpus", "1", "--memory", "1536m", "--memory-swap", "1536m", "--no-healthcheck",
                  "-v", f"{PROJECT}:/workspace:ro", "-v", f"{ROOT / 'data'}:/latency-data",
                  "-e", "LATENCY_ACCEPTANCE=isolated-nas-v1", "-e", "PYTHONDONTWRITEBYTECODE=1",
                  "-e", "MEILI_URL=http://latency-meili:7700", "-e", "MEILI_MASTER_KEY=latency-bench-search-key",
                  "-e", "SECRET_KEY=latency-benchmark-isolated-secret-2026-only", "-e", "ADMIN_PASSWORD=latency-benchmark-only",
                  "-e", "GALLERYDL_CONFIG_ROOT=/latency-data/gallery-config", "-e", "RESOURCE_GOVERNANCE_MODE=enforce",
                  "-e", "RESOURCE_GOVERNANCE_MAX_SCALE=1.0", IMAGE, "sleep", "infinity"])
    (ROOT / "state.json").write_text(json.dumps({"revisions": revisions, "image": IMAGE, "services": SERVICES, "network": NETWORK}, indent=2))
    for _ in range(30):
        probe = run(["docker", "exec", SERVICES["postgres"], "pg_isready", "-U", "autogallery", "-d", "latency_template"], check=False)
        if probe.returncode == 0:
            break
        time.sleep(1)
    execute("baseline", ["alembic", "upgrade", "head"], database="latency_template", log=ROOT / "reports" / "migration-baseline.log")
    execute("baseline", ["python", "/workspace/backend/scripts/latency_acceptance.py", "seed"], database="latency_template", log=ROOT / "reports" / "seed.log")
    for variant in ("baseline", "candidate"):
        run(["docker", "exec", SERVICES["postgres"], "createdb", "-U", "autogallery", "-T", "latency_template", f"latency_{variant}"])
    execute("candidate", ["alembic", "upgrade", "head"], log=ROOT / "reports" / "migration-candidate.log")
    start_apis()
    print(json.dumps({"event": "ready", "root": str(ROOT), "revisions": revisions}), flush=True)


def start_apis():
    for variant in ("baseline", "candidate"):
        name = f"ag-latency-bench-api-{variant}"
        args = ["docker", "run", "-d", "--network", NETWORK, "--label", LABEL, "--pids-limit", "128",
                "--name", name, "--network-alias", f"latency-api-{variant}", "--cpus", "0.5", "--memory", "512m", "--memory-swap", "512m", "--no-healthcheck",
                "-v", f"{PROJECT}:/workspace:ro", "-v", f"{ROOT / 'data'}:/latency-data", "-w", source(variant)]
        env = {"PYTHONPATH": source(variant), "PYTHONDONTWRITEBYTECODE": "1",
               "DATABASE_URL": f"postgresql+asyncpg://autogallery:latency-bench-db@latency-postgres:5432/latency_{variant}",
               "REDIS_URL": f"redis://latency-redis:6379/{1 if variant == 'baseline' else 2}",
               "MEILI_URL": "http://latency-meili:7700", "MEILI_MASTER_KEY": "latency-bench-search-key",
               "MEILI_INDEX_PREFIX": f"latency_{variant}_", "SECRET_KEY": "latency-benchmark-isolated-secret-2026-only",
               "ADMIN_PASSWORD": "latency-benchmark-only", "DOWNLOAD_ROOT": f"/latency-data/{variant}/downloads",
               "LIBRARY_ROOT": f"/latency-data/{variant}/library", "APP_CONFIG_ROOT": f"/latency-data/{variant}/app-config"}
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        run(args + [IMAGE, "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--lifespan", "off", "--no-access-log"])


def trials(repetitions, smoke, identity_base):
    if not (ROOT / "state.json").is_file():
        raise RuntimeError("Run setup first")
    shapes = [(9, 27)] if smoke else [(9, 27), (18, 38), (4, 277)]
    for repetition in range(repetitions + 1):
        for shape, (works, assets) in enumerate(shapes):
            order = ("baseline", "candidate") if repetition % 2 == 0 else ("candidate", "baseline")
            for variant in order:
                identity = identity_base + repetition * 100 + shape
                label = f"{variant}-{works}-{assets}-{repetition}"
                output = ROOT / "reports" / f"{label}.log"
                result_path = ROOT / "reports" / f"{label}.json"
                if result_path.exists():
                    continue
                command = ["python", "/workspace/backend/scripts/latency_acceptance.py", "trial", "--variant", variant,
                           "--works", str(works), "--assets", str(assets), "--identity", str(identity), "--repetition", str(repetition), "--browse"]
                execute(variant, command, log=output)
                records = [json.loads(line) for line in output.read_text().splitlines() if line.startswith('{"event": "trial"')]
                if len(records) != 1:
                    raise RuntimeError(f"Expected one completed trial in {output}")
                record = records[0]
                result_path.write_text(json.dumps(record, indent=2))
                print(json.dumps({key: record[key] for key in ("variant", "repetition", "works", "assets", "creation_seconds", "promotion_seconds", "import_seconds")}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("setup", "run"))
    parser.add_argument("--candidate", default="HEAD")
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--identity-base", type=int, default=9200000)
    args = parser.parse_args()
    if args.command == "setup":
        setup(args.candidate)
    else:
        trials(args.repetitions, args.smoke, args.identity_base)


if __name__ == "__main__":
    main()
