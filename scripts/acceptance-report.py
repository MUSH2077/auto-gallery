#!/usr/bin/env python3
"""Create machine-readable and human-readable acceptance manifests."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import subprocess

PHASES = ("build", "seed", "functional", "fault", "performance", "coexistence")


def _image_id(reference: str) -> str | None:
    result = subprocess.run(
        ["docker", "image", "inspect", reference, "--format", "{{.Id}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--scope", choices=("core", "full"), default="full")
    args = parser.parse_args()
    root = Path(args.root).resolve(strict=True)
    if not (root / ".auto-gallery-test-root").is_file():
        parser.error("not a marked acceptance root")
    digest = (root / "source-digest.txt").read_text().strip()
    phases = {name: (root / f"{name}.pass").is_file() for name in PHASES}
    scale_ok = False
    try:
        works, assets, tag_relations, artifacts = (
            int(value) for value in (root / "scale.csv").read_text().strip().split(",")
        )
        scale_ok = (
            works >= 67_000
            and assets >= 90_000
            and tag_relations >= 470_000
            and artifacts >= 330_000
        )
    except (FileNotFoundError, ValueError):
        works = assets = tag_relations = artifacts = 0
    coexistence_ok = False
    try:
        coexistence_ok = (
            json.loads((root / "reports/coexistence-summary.json").read_text()).get("result")
            == "pass"
        )
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    discovery_ok = False
    try:
        discovery_ok = (
            json.loads((root / "reports/discovery-scaling.json").read_text()).get("result")
            == "pass"
        )
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    guardian_files = sorted((root / "reports").glob("*guardian.jsonl"))
    guardian_ok = bool(guardian_files)
    for path in guardian_files:
        try:
            for line in path.read_text().splitlines():
                if line.strip() and json.loads(line).get("hard_reasons"):
                    guardian_ok = False
        except (OSError, json.JSONDecodeError):
            guardian_ok = False
    backend_image = _image_id(f"auto-gallery-backend:candidate-{digest}")
    admin_image = _image_id(f"auto-gallery-admin-web:candidate-{digest}")
    evidence = {
        "scale": scale_ok,
        "coexistence_summary": coexistence_ok,
        "guardian_clean": guardian_ok,
        "migration_round_trip": (root / "sanitized-schema.sha256").is_file(),
        "runtime": (root / "runtime.pass").is_file(),
        "core_smoke": (root / "core-smoke.pass").is_file(),
        "controller_scenarios": (root / "reports/controller-scenarios.json").is_file(),
        "discovery_scaling": discovery_ok,
        "candidate_images_present": bool(backend_image and admin_image),
        "raw_snapshot_removed": not (root / "production.raw.dump").exists(),
    }
    if args.scope == "core":
        required_phases = ("build", "seed", "functional", "fault")
        required_evidence = (
            "scale",
            "guardian_clean",
            "migration_round_trip",
            "runtime",
            "core_smoke",
            "candidate_images_present",
            "raw_snapshot_removed",
        )
    else:
        required_phases = PHASES
        required_evidence = tuple(evidence)
    requirements = {
        "phases": {name: phases[name] for name in required_phases},
        "evidence": {name: evidence[name] for name in required_evidence},
    }
    result = (
        "pass"
        if all(requirements["phases"].values())
        and all(requirements["evidence"].values())
        else "fail"
    )
    generated = datetime.now(timezone.utc)
    payload = {
        "schema_version": 2,
        "result": result,
        "deployment_scope": args.scope,
        "generated_at": generated.isoformat(),
        "valid_until": generated.timestamp() + 24 * 60 * 60,
        "project": args.project,
        "source_digest": digest,
        "images": {
            "backend": backend_image,
            "admin_web": admin_image,
            "backend_base": (
                (root / "backend-base-image-id.txt").read_text().strip()
                if (root / "backend-base-image-id.txt").is_file()
                else None
            ),
        },
        "scale": {
            "works": works,
            "assets": assets,
            "tag_relations": tag_relations,
            "artifacts": artifacts,
        },
        "evidence": evidence,
        "gitllery": {"product_version": "v1", "projection_mode": "shadow"},
        "phases": phases,
        "requirements": requirements,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
    (root / "acceptance.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    rows = "".join(
        f"<tr><td>{html.escape(name)}</td><td>{'PASS' if passed else 'FAIL'}</td></tr>"
        for name, passed in {**phases, **evidence}.items()
    )
    (root / "acceptance.html").write_text(
        "<!doctype html><meta charset=utf-8><title>auto-gallery acceptance</title>"
        f"<h1>Acceptance: {result.upper()}</h1><p>Source: <code>{html.escape(digest)}</code></p>"
        f"<table><thead><tr><th>Phase</th><th>Result</th></tr></thead><tbody>{rows}</tbody></table>",
        encoding="utf-8",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if result == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
