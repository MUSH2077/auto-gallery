#!/usr/bin/env python3
"""Build a separate, bounded NAS fixture and compare immutable source snapshots.

This never connects to production services. PostgreSQL/Redis/Meili run on an
internal network; all writable mounts live below a marked acceptance root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / ".superpowers" / "latency-acceptance"
IMAGE = "auto-gallery-backend:candidate-e7a11de3b705168ce6871c8c10fbba9045d5f71b18282aab3e1b3ebdefbeda2f"
NETWORK = "ag-latency-bench"
LABEL = "codex.task=latency-acceptance"
SERVICES = {name: f"ag-latency-bench-{name}" for name in ("postgres", "redis", "meili", "runner")}


def harness_hashes():
    return {"driver": hashlib.sha256((PROJECT / "backend/scripts/latency_acceptance.py").read_bytes()).hexdigest(),
            "orchestrator": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def validate_run(state):
    if not state.get("run_id") or state.get("harness_hashes") != harness_hashes():
        raise RuntimeError("Harness is unsealed or changed; preserve preliminary evidence and seal a fresh run")


def validate_record(state, record, *, variant=None, repetition=None, works=None, assets=None):
    validate_run(state)
    expected = {"event": "trial", "run_id": state["run_id"],
                "harness_sha256": state["harness_hashes"]["driver"],
                "orchestrator_sha256": state["harness_hashes"]["orchestrator"]}
    expected.update({key: value for key, value in {
        "variant": variant, "repetition": repetition, "works": works, "assets": assets}.items() if value is not None})
    expected["source_revision"] = state["revisions"].get(record.get("variant"))
    for key, value in expected.items():
        if record.get(key) != value or value is None:
            raise RuntimeError(f"Measurement does not match sealed run: {key}")


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
    state = json.loads((ROOT / "state.json").read_text())
    env = {
        "PYTHONPATH": source(variant),
        "DATABASE_URL": f"postgresql+asyncpg://autogallery:latency-bench-db@latency-postgres:5432/{database or 'latency_' + variant}",
        "REDIS_URL": f"redis://latency-redis:6379/{state.get('redis_databases', {'baseline': 1, 'candidate': 2})[variant]}",
        "MEILI_INDEX_PREFIX": state.get("index_prefixes", {}).get(variant, f"latency_{variant}_"),
        "DOWNLOAD_ROOT": f"/latency-data/{variant}/downloads",
        "LIBRARY_ROOT": f"/latency-data/{variant}/library",
        "APP_CONFIG_ROOT": f"/latency-data/{variant}/app-config",
        "LATENCY_BROWSE_URL": f"http://latency-api-{variant}:8000",
        "LATENCY_SOURCE_REVISION": state["revisions"][variant],
        "LATENCY_ORCHESTRATOR_SHA256": harness_hashes()["orchestrator"],
        "LATENCY_RUN_ID": state.get("run_id", "preliminary"),
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
    (ROOT / "state.json").write_text(json.dumps({"revisions": revisions, "image": IMAGE, "services": SERVICES, "network": NETWORK,
                                               "run_id": str(uuid4()), "harness_hashes": harness_hashes()}, indent=2))
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
    state = json.loads((ROOT / "state.json").read_text())
    for variant in ("baseline", "candidate"):
        name = f"ag-latency-bench-api-{variant}"
        args = ["docker", "run", "-d", "--network", NETWORK, "--label", LABEL, "--pids-limit", "128",
                "--name", name, "--network-alias", f"latency-api-{variant}", "--cpus", "0.5", "--memory", "512m", "--memory-swap", "512m", "--no-healthcheck",
                "-v", f"{PROJECT}:/workspace:ro", "-v", f"{ROOT / 'data'}:/latency-data", "-w", source(variant)]
        env = {"PYTHONPATH": source(variant), "PYTHONDONTWRITEBYTECODE": "1",
               "DATABASE_URL": f"postgresql+asyncpg://autogallery:latency-bench-db@latency-postgres:5432/latency_{variant}",
               "REDIS_URL": f"redis://latency-redis:6379/{state.get('redis_databases', {'baseline': 1, 'candidate': 2})[variant]}",
               "MEILI_URL": "http://latency-meili:7700", "MEILI_MASTER_KEY": "latency-bench-search-key",
               "MEILI_INDEX_PREFIX": state.get("index_prefixes", {}).get(variant, f"latency_{variant}_"), "SECRET_KEY": "latency-benchmark-isolated-secret-2026-only",
               "ADMIN_PASSWORD": "latency-benchmark-only", "DOWNLOAD_ROOT": f"/latency-data/{variant}/downloads",
               "LIBRARY_ROOT": f"/latency-data/{variant}/library", "APP_CONFIG_ROOT": f"/latency-data/{variant}/app-config"}
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        run(args + [IMAGE, "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--lifespan", "off", "--no-access-log"])


def seal(candidate):
    """Preserve preliminary fixtures, then clone a matched final comparison."""
    state = json.loads((ROOT / "state.json").read_text())
    if state["network"] != NETWORK or state["services"] != SERVICES or not (ROOT / "data/.latency-acceptance").is_file():
        raise RuntimeError("Acceptance isolation guard failed")
    if run(["git", "-C", str(PROJECT), "status", "--porcelain", "--untracked-files=no"]).stdout.strip():
        raise RuntimeError("Commit reviewed code before sealing the comparison")
    processes = run(["docker", "top", SERVICES["runner"], "-eo", "args"]).stdout.splitlines()[1:]
    if processes != ["sleep infinity"]:
        raise RuntimeError(f"Acceptance runner must be idle before sealing: {processes}")
    names = [*SERVICES.values(), *(f"ag-latency-bench-api-{v}" for v in ("baseline", "candidate"))]
    for name in names:
        detail = json.loads(run(["docker", "inspect", name]).stdout)[0]
        if detail["Config"]["Labels"].get("codex.task") != "latency-acceptance":
            raise RuntimeError(f"Refusing unmarked container {name}")
    next_db = max(state.get("redis_databases", {"baseline": 1, "candidate": 2}).values()) + 1
    if next_db + 1 >= 16:
        raise RuntimeError("No unused isolated Redis namespace remains")
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    archive = ROOT / f"preliminary-{stamp}"
    archive.mkdir()
    (archive / "state.json").write_text(json.dumps(state, indent=2))
    for variant in ("baseline", "candidate"):
        name = f"ag-latency-bench-api-{variant}"
        run(["docker", "stop", "--time", "10", name])
        run(["docker", "rm", name])
        run(["docker", "exec", SERVICES["postgres"], "psql", "-U", "autogallery", "-d", "latency_template", "-v", "ON_ERROR_STOP=1", "-c",
             f"ALTER DATABASE latency_{variant} RENAME TO latency_{variant}_{stamp.lower()}"])
        (ROOT / "data" / variant).rename(archive / f"data-{variant}")
        for folder in ("downloads", "library", "app-config"):
            (ROOT / "data" / variant / folder).mkdir(parents=True, exist_ok=True)
    (ROOT / "reports").rename(archive / "reports")
    (ROOT / "reports").mkdir()
    (ROOT / "source-candidate").rename(archive / "source-candidate")
    if (ROOT / "candidate.tar").exists():
        (ROOT / "candidate.tar").rename(archive / "candidate.tar")
    state["revisions"]["candidate"] = snapshot(candidate, "candidate")
    state.update(run_id=str(uuid4()), harness_hashes=harness_hashes(),
                 redis_databases={"baseline": next_db, "candidate": next_db + 1},
                 index_prefixes={v: f"latency_{stamp.lower()}_{v}_" for v in ("baseline", "candidate")},
                 preliminary_archive=str(archive))
    (ROOT / "state.json").write_text(json.dumps(state, indent=2))
    for variant in ("baseline", "candidate"):
        run(["docker", "exec", SERVICES["postgres"], "createdb", "-U", "autogallery", "-T", "latency_template", f"latency_{variant}"])
    execute("candidate", ["alembic", "upgrade", "head"], log=ROOT / "reports/migration-candidate.log")
    start_apis()
    print(json.dumps({"event": "sealed", "run_id": state["run_id"], "revisions": state["revisions"], "archive": str(archive)}), flush=True)


def trials(repetitions, smoke, identity_base):
    if not (ROOT / "state.json").is_file():
        raise RuntimeError("Run setup first")
    state = json.loads((ROOT / "state.json").read_text())
    validate_run(state)
    shapes = [(9, 27)] if smoke else [(9, 27), (18, 38), (4, 277)]
    valid_pairs = {shape: 0 for shape in shapes}
    for repetition in range(max(1, repetitions * 3 + 1)):
        for shape, (works, assets) in enumerate(shapes):
            if repetition > 0 and valid_pairs[(works, assets)] >= repetitions:
                continue
            order = ("baseline", "candidate") if repetition % 2 == 0 else ("candidate", "baseline")
            pair = []
            for variant in order:
                identity = identity_base + repetition * 100 + shape
                label = f"{variant}-{works}-{assets}-{repetition}"
                output = ROOT / "reports" / f"{label}.log"
                result_path = ROOT / "reports" / f"{label}.json"
                if result_path.exists():
                    record = json.loads(result_path.read_text())
                    validate_record(state, record, variant=variant, repetition=repetition, works=works, assets=assets)
                    pair.append(record)
                    continue
                command = ["python", "/workspace/backend/scripts/latency_acceptance.py", "trial", "--variant", variant,
                           "--works", str(works), "--assets", str(assets), "--identity", str(identity), "--repetition", str(repetition), "--browse"]
                execute(variant, command, log=output)
                records = [json.loads(line) for line in output.read_text().splitlines() if line.startswith('{"event": "trial"')]
                if len(records) != 1:
                    raise RuntimeError(f"Expected one completed trial in {output}")
                record = records[0]
                validate_record(state, record, variant=variant, repetition=repetition, works=works, assets=assets)
                result_path.write_text(json.dumps(record, indent=2))
                pair.append(record)
                print(json.dumps({key: record[key] for key in ("variant", "repetition", "works", "assets", "creation_seconds", "promotion_seconds", "import_seconds")}), flush=True)
            if repetition > 0 and all(row["normal_resource"] for row in pair):
                if len({row["harness_sha256"] for row in pair}) != 1:
                    raise RuntimeError("Harness changed within a comparison pair")
                valid_pairs[(works, assets)] += 1
        if repetition == 0 and repetitions == 0 or all(count >= repetitions for count in valid_pairs.values()):
            break
    if any(count < repetitions for count in valid_pairs.values()):
        raise RuntimeError(f"Insufficient valid normal-resource pairs: {valid_pairs}")


def summarize():
    state = json.loads((ROOT / "state.json").read_text())
    validate_run(state)
    rows = []
    for path in (ROOT / "reports").glob("*-*-*-*.json"):
        row = json.loads(path.read_text())
        if row.get("event") == "trial" and row.get("repetition", 0) > 0:
            validate_record(state, row)
            rows.append(row)

    def p95(values):
        return sorted(values)[math.ceil(len(values) * .95) - 1] if values else None

    report = {"method": "nearest-rank p95, first 20 matched noncritical pairs, warmup excluded", "revisions": state["revisions"], "scenarios": []}
    for works, assets, target in ((9, 27, 30), (18, 38, 60), (4, 277, 90)):
        paired = []
        by_rep = {}
        for row in rows:
            if (row["works"], row["assets"]) == (works, assets):
                by_rep.setdefault(row["repetition"], {})[row["variant"]] = row
        for repetition, variants in sorted(by_rep.items()):
            if set(variants) != {"baseline", "candidate"}:
                continue
            pair = list(variants.values())
            if not all(row["normal_resource"] and row["source_revision"] == state["revisions"][row["variant"]] for row in pair):
                continue
            if len({(row["harness_sha256"], row["image_sha256"], row["category"], row["read_category"]) for row in pair}) != 1:
                raise RuntimeError(f"Non-equivalent fixtures in repetition {repetition}")
            paired.append(variants)
            if len(paired) == 20:
                break
        scenario = {"works": works, "assets": assets, "valid_pairs": len(paired), "target_import_seconds": target}
        for variant in ("baseline", "candidate"):
            selected = [pair[variant] for pair in paired]
            scenario[variant] = {key + "_p95": p95([row[key] for row in selected]) for key in ("creation_seconds", "promotion_seconds", "import_seconds")}
            scenario[variant]["browse_seconds_p95"] = p95([sample["seconds"] for row in selected for sample in row["browse"]])
            scenario[variant]["browse_samples"] = sum(len(row["browse"]) for row in selected)
        candidate, baseline = scenario["candidate"], scenario["baseline"]
        scenario["timing_targets_passed"] = len(paired) == 20 and candidate["creation_seconds_p95"] <= 1 and candidate["import_seconds_p95"] <= target and (
            assets != 277 or candidate["promotion_seconds_p95"] <= 30) and candidate["browse_seconds_p95"] <= 1.2 * baseline["browse_seconds_p95"]
        report["scenarios"].append(scenario)
    report["timing_targets_passed"] = all(row["timing_targets_passed"] for row in report["scenarios"])
    (ROOT / "reports" / "comparison.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("setup", "seal", "run", "summarize"))
    parser.add_argument("--candidate", default="HEAD")
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--identity-base", type=int, default=9200000)
    args = parser.parse_args()
    if args.repetitions < 0:
        parser.error("repetitions must be nonnegative")
    if args.command == "setup":
        setup(args.candidate)
    elif args.command == "seal":
        seal(args.candidate)
    elif args.command == "run":
        trials(args.repetitions, args.smoke, args.identity_base)
    else:
        summarize()


if __name__ == "__main__":
    main()
