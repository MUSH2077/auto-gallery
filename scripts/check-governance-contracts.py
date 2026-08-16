#!/usr/bin/env python3
"""Static safety contracts shared by local acceptance and CI."""

from __future__ import annotations

import ast
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "backend/app"
RAW_QUEUE_ALLOWLIST = {
    APP / "services/backpressure.py",
    APP / "services/queue_admission.py",
}


def main() -> int:
    failures: list[str] = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr in {"enqueue", "enqueue_in", "enqueue_at"} and path not in RAW_QUEUE_ALLOWLIST:
                failures.append(f"raw RQ {node.func.attr}: {path.relative_to(ROOT)}:{node.lineno}")

    version_pattern = re.compile(r"gitllery(?:[-_ ]|\s+)v(?:2|3)|Gitllery\s+v(?:2|3)", re.I)
    for base in (ROOT / "backend/gitllery_cli", ROOT / "backend/gitllery_format", ROOT / "docs/gitllery-v1.md"):
        paths = [base] if base.is_file() else list(base.rglob("*.py"))
        for path in paths:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if version_pattern.search(line):
                    failures.append(f"product version drift: {path.relative_to(ROOT)}:{number}")

    gitllery_settings_sources = (
        ROOT / "backend/app/api/admin/settings.py",
        ROOT / "backend/app/schemas/gitllery.py",
        ROOT / "admin-web/src/app/admin/settings/gitllery/page.tsx",
    )
    for path in gitllery_settings_sources:
        source = path.read_text(encoding="utf-8")
        if version_pattern.search(source):
            failures.append(f"Gitllery settings exposes a non-v1 product name: {path.relative_to(ROOT)}")
    settings_page = gitllery_settings_sources[-1].read_text(encoding="utf-8")
    if settings_page.count("useQuery({") != 1:
        failures.append("Gitllery settings must use one cached settings/status query")
    if "refetchOnWindowFocus: false" not in settings_page or "staleTime: 60_000" not in settings_page:
        failures.append("Gitllery settings query cache contract is missing")
    if "useEffect(" in settings_page:
        failures.append("Gitllery settings must derive display state during render")
    if "updateAdminSettings" in settings_page or "gitlleryReconcile" in settings_page or "gitlleryRebuild" in settings_page:
        failures.append("Gitllery settings must not expose deployment or full-history mutations")
    api_settings = gitllery_settings_sources[0].read_text(encoding="utf-8")
    if '@router.get("/gitllery/settings"' not in api_settings:
        failures.append("read-only Gitllery settings endpoint is missing")
    if '@router.put("/gitllery/settings"' in api_settings or '@router.post("/gitllery/settings"' in api_settings:
        failures.append("Gitllery projection mode must not be mutable from the settings API")
    creator_panel = (ROOT / "admin-web/src/components/GitlleryPanel.tsx").read_text(encoding="utf-8")
    if "gitlleryReconcile" in creator_panel or "gitlleryRebuild" in creator_panel:
        failures.append("creator Gitllery summary must not duplicate global maintenance actions")

    guardian = (ROOT / "scripts/test-guardian.py").read_text(encoding="utf-8")
    for policy in ("build-functional", "load-soak", "deploy-core"):
        if policy not in guardian:
            failures.append(f"acceptance guardian is missing policy {policy}")
    if 'hard.append("sustained_psi")' in guardian:
        failures.append("host PSI must not be an unattributed hard failure")
    if 'hard.append("attributed_sustained_pressure")' not in guardian:
        failures.append("relative PSI attribution gate is missing")

    deploy = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    for contract in (
        "--verified",
        'DEPLOY_MODE="local"',
        "build_local_candidate",
        "resolve_acceptance_manifest",
        "VERIFY_SCOPE=core",
        "VERIFY_SCOPE=full",
    ):
        if contract not in deploy:
            failures.append(f"portable deployment contract is missing: {contract}")
    for forbidden in (
        "MEM_RESUME_KB=$((1536 * 1024))",
        "SWAP_RESUME_PERCENT=25",
        "wait_for_host_resources",
        "photo_serv",
        "search_serv",
        "earlyoom",
    ):
        if forbidden in deploy:
            failures.append(f"deployment still contains a host-specific veto: {forbidden}")
    if 'if [[ "$DEPLOY_MODE" == "verified" ]]' not in deploy:
        failures.append("default deploy must not read acceptance state")

    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    for variable in (
        "POSTGRES_MEM_LIMIT",
        "BACKEND_MEM_LIMIT",
        "DOWNLOAD_WORKER_MEM_LIMIT",
        "IMPORT_WORKER_MEM_LIMIT",
        "OPERATIONS_WORKER_MEM_LIMIT",
        "ADMIN_WEB_MEM_LIMIT",
    ):
        if f"${{{variable}:-" not in compose:
            failures.append(f"Compose resource cap is not device-overridable: {variable}")

    source_roots = (ROOT / "backend/app", ROOT / "admin-web/src")
    legacy_allowed = ROOT / "admin-web/src/lib/slideshow/config.tsx"
    for base in source_roots:
        for path in base.rglob("*"):
            if not path.is_file() or path == legacy_allowed:
                continue
            if "showcase" in path.read_text(encoding="utf-8", errors="ignore").lower():
                failures.append(f"removed showcase feature remains in {path.relative_to(ROOT)}")
    report = (ROOT / "scripts/acceptance-report.py").read_text(encoding="utf-8")
    if 'choices=("core", "full")' not in report or '"deployment_scope": args.scope' not in report:
        failures.append("acceptance manifest deployment scope contract is missing")

    if failures:
        print("\n".join(failures))
        return 1
    print("governance contracts: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
