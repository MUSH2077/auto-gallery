import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _extract_shell_function(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.index("\n}\n", start) + 2
    return source[start:end]


def _source_digest_for_locale(repo: Path, locale_name: str) -> str:
    source = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    harness = "\n\n".join(
        _extract_shell_function(source, name)
        for name in ("existing_source_paths", "source_digest")
    )
    result = subprocess.run(
        ["bash", "-c", f"set -Eeuo pipefail\n{harness}\nsource_digest"],
        cwd=repo,
        env={**os.environ, "LC_ALL": locale_name},
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    return result.stdout.strip()


def _run_deploy_rollback(tmp_path: Path, current_revision: str):
    """Run the production rollback entrypoint against a recording Docker fake."""

    rollback_dir = tmp_path / "rollback"
    project_root = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    rollback_dir.mkdir()
    project_root.mkdir()
    fake_bin.mkdir()
    rollback_script = ROOT / "scripts/deploy-rollback.sh"
    shutil.copy2(rollback_script, rollback_dir / "rollback.sh")
    (rollback_dir / "rollback.sh").chmod(0o700)
    (rollback_dir / ".env.predeploy").write_text("SECRET_KEY=test-only\n", encoding="utf-8")
    (rollback_dir / "docker-compose.candidate.yaml").write_text("services: {}\n", encoding="utf-8")

    predeploy_revision = "111111111111"
    candidate_revision = "222222222222"
    manifest = {
        "DEPLOYMENT_ID": "rollback-contract",
        "PROJECT_ROOT": str(project_root),
        "PREDEPLOY_GIT_HEAD": "predeploy-git-head",
        "PREDEPLOY_ALEMBIC_REVISION": predeploy_revision,
        "CANDIDATE_ALEMBIC_REVISION": candidate_revision,
        "BACKEND_IMAGE_ID": "sha256:old-backend",
        "BACKEND_ROLLBACK_TAG": "auto-gallery-backend:rollback-contract",
        "ADMIN_IMAGE_ID": "sha256:old-admin",
        "ADMIN_ROLLBACK_TAG": "auto-gallery-admin-web:rollback-contract",
        "CANDIDATE_BACKEND_IMAGE": "auto-gallery-backend:candidate-contract",
        "CANDIDATE_BACKEND_IMAGE_ID": "sha256:candidate-backend",
        "CANDIDATE_ADMIN_IMAGE": "auto-gallery-admin-web:candidate-contract",
        "CANDIDATE_ADMIN_IMAGE_ID": "sha256:candidate-admin",
        "ROLLBACK_SCHEMA_POLICY": "schema-forward",
        "ROLLBACK_SCHEMA_CURRENT_REVISION_AT_SNAPSHOT": predeploy_revision,
        "ROLLBACK_SCHEMA_RETAIN_CANDIDATE": "true",
        "ROLLBACK_OLD_MIGRATE_ONLY_AT_PREDEPLOY": "true",
    }
    (rollback_dir / "manifest.env").write_text(
        "".join(f"{key}={shlex.quote(value)}\n" for key, value in manifest.items()),
        encoding="utf-8",
    )

    log_path = tmp_path / "docker.log"
    (fake_bin / "docker").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$ROLLBACK_TEST_LOG"
if [[ "$*" == *"exec -T postgres sh -c"* ]]; then
  printf '%s\\n' "$ROLLBACK_TEST_CURRENT_REVISION"
fi
if [[ "${ROLLBACK_TEST_REJECT_OLD_MIGRATE:-0}" == "1" \
      && "$*" == *"up --force-recreate --no-deps --no-build migrate"* ]]; then
  exit 91
fi
""",
        encoding="utf-8",
    )
    (fake_bin / "docker").chmod(0o700)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ROLLBACK_TEST_LOG": str(log_path),
        "ROLLBACK_TEST_CURRENT_REVISION": current_revision,
        # A retained candidate schema may be unknown to the old image. Make
        # accidental old-migrate execution fatal in the behavioral contract.
        "ROLLBACK_TEST_REJECT_OLD_MIGRATE": "1" if current_revision == candidate_revision else "0",
    }
    result = subprocess.run(
        [str(rollback_dir / "rollback.sh")],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    receipt_path = rollback_dir / "rollback-receipt.env"
    receipt = {}
    if receipt_path.exists():
        receipt = dict(
            line.split("=", 1)
            for line in receipt_path.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
    return result, log, receipt, predeploy_revision, candidate_revision


def test_default_deploy_does_not_resolve_acceptance_or_gate_on_host_pressure():
    source = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    mode_branch = source.split("# ── 1. Candidate", 1)[1].split("# ── 2.", 1)[0]

    assert 'if [[ "$DEPLOY_MODE" == "verified" ]]' in mode_branch
    assert mode_branch.index("resolve_acceptance_manifest") < mode_branch.index("else")
    assert mode_branch.index("build_local_candidate") > mode_branch.index("else")
    assert 'if [[ "$DEPLOY_MODE" == "verified" && -s "$ACCEPTANCE_MANIFEST" ]]' in source
    for forbidden in (
        "wait_for_host_resources",
        "MEM_RESUME_KB",
        "SWAP_RESUME_PERCENT",
        "photo_serv",
        "earlyoom",
    ):
        assert forbidden not in source


def test_local_builder_is_serialized_and_prefers_a_no_swap_project_cgroup():
    source = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")

    assert "COMPOSE_PARALLEL_LIMIT=1" in source
    assert '--driver docker-container' in source
    assert '--driver-opt "memory=${LOCAL_BUILD_MEMORY_LIMIT:-3072m}"' in source
    assert '--driver-opt "memory-swap=${LOCAL_BUILD_MEMORY_LIMIT:-3072m}"' in source
    assert 'docker buildx rm "$builder_name"' in source


def test_frontend_build_separates_and_requires_typechecking():
    dockerfile = (ROOT / "admin-web/Dockerfile").read_text(encoding="utf-8")
    next_config = (ROOT / "admin-web/next.config.js").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")

    assert "ENV NEXT_SKIP_INTERNAL_TYPECHECK=1" in dockerfile
    assert "FROM deps AS builder" in dockerfile
    assert "COPY docs/api/openapi.json /docs/api/openapi.json" in dockerfile
    assert "FROM builder AS checks" in dockerfile
    assert "npm run typecheck" in dockerfile
    assert "touch /tmp/admin-web-checks.pass" in dockerfile
    assert "COPY --from=checks /tmp/admin-web-checks.pass" in dockerfile
    assert (
        "ignoreBuildErrors: process.env.NEXT_SKIP_INTERNAL_TYPECHECK === '1'"
        in next_config
    )
    assert "context: .\n      dockerfile: admin-web/Dockerfile" in compose
    assert "ADMIN_BUILD_NODE_HEAP_MB: ${ADMIN_BUILD_NODE_HEAP_MB:-640}" in compose


def test_backend_candidate_resets_pythonpath_to_the_replaced_source_tree():
    dockerfile = (ROOT / "backend/Dockerfile.candidate").read_text(encoding="utf-8")

    # Thin production images deliberately import from /candidate-app.  The
    # layered candidate replaces the application beneath /app, so it must also
    # replace the inherited import root or workers keep executing stale code.
    assert "PYTHONPATH=/app" in dockerfile


def test_source_digest_and_snapshot_skip_tracked_deletions():
    deploy = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    acceptance = (ROOT / "scripts/test-env.sh").read_text(encoding="utf-8")

    for source in (deploy, acceptance):
        assert "existing_source_paths" in source
        assert '[[ -f "$path" || -L "$path" ]]' in source
    assert "existing_source_paths | \\" in deploy
    assert "tar --null --files-from=-" in deploy


def test_source_digest_is_stable_across_available_collations(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    for name in ("A", "a", "_a", "á"):
        (repo / name).write_text(f"contents for {name}\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "A", "a", "_a", "á"], cwd=repo, check=True)

    installed_locales = subprocess.run(
        ["locale", "-a"], capture_output=True, text=True, timeout=5, check=True
    ).stdout.splitlines()
    unicode_locale = next(
        (
            name
            for name in installed_locales
            if name.lower().replace("-", "").replace("_", "").replace(".", "")
            == "enusutf8"
        ),
        None,
    )
    if unicode_locale is None:
        pytest.skip("en_US UTF-8 locale is not installed")

    c_digest = _source_digest_for_locale(repo, "C")
    unicode_digest = _source_digest_for_locale(repo, unicode_locale)

    assert unicode_digest == c_digest


def test_project_backup_and_core_health_failures_are_fail_closed():
    source = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")

    assert "check_backup_capacity" in source
    assert "pg_database_size(current_database())" in source
    assert 'if "$ROLLBACK_DIR/rollback.sh"' in source
    assert "Automatic foreground rollback completed" in source
    assert "DEPLOY_MUTATION_STARTED=0" in source
    assert 'if [[ "$DEPLOY_MUTATION_STARTED" -eq 1 ]]' in source


def test_automatic_failure_trap_executes_the_frozen_rollback_entrypoint(tmp_path):
    """A post-mutation deploy error must invoke the same snapshotted renderer."""

    source = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    start = source.index("deploy_failed() {")
    end = source.index("\n}\n\ntrap deploy_failed ERR", start) + 2
    failure_function = source[start:end]
    rollback_dir = tmp_path / "rollback"
    rollback_dir.mkdir()
    marker = tmp_path / "rollback-invoked"
    (rollback_dir / "rollback.sh").write_text(
        f"#!/usr/bin/env bash\nprintf invoked >{shlex.quote(str(marker))}\n",
        encoding="utf-8",
    )
    (rollback_dir / "rollback.sh").chmod(0o700)
    harness = tmp_path / "failure-harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "RED=''\nNC=''\n"
        "DEPLOY_MUTATION_STARTED=1\nROLLBACK_READY=1\n"
        f"ROLLBACK_DIR={shlex.quote(str(rollback_dir))}\n"
        "compose() { return 0; }\n"
        f"{failure_function}\n"
        "trap deploy_failed ERR\n"
        "false\n",
        encoding="utf-8",
    )
    harness.chmod(0o700)

    result = subprocess.run(
        [str(harness)], capture_output=True, text=True, timeout=5
    )

    assert result.returncode == 1
    assert marker.read_text(encoding="utf-8") == "invoked"
    assert "Automatic foreground rollback completed" in result.stderr


def test_rollback_commands_override_frozen_custom_image_configuration():
    """Rollback must use snapshotted image identities, not .env image aliases."""
    deploy = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    rollback = (ROOT / "scripts/deploy-rollback.sh").read_text(encoding="utf-8")

    assert (
        'BACKEND_IMAGE="$BACKEND_IMAGE_ID" ADMIN_IMAGE="$ADMIN_IMAGE_ID" \\\n'
        "    docker compose"
    ) in rollback
    assert "scripts/deploy-rollback.sh" in deploy
    for field in (
        "ROLLBACK_SCHEMA_POLICY",
        "ROLLBACK_SCHEMA_CURRENT_REVISION_AT_SNAPSHOT",
        "ROLLBACK_SCHEMA_RETAIN_CANDIDATE",
        "ROLLBACK_OLD_MIGRATE_ONLY_AT_PREDEPLOY",
    ):
        assert field in deploy
    assert "alembic downgrade" not in deploy
    assert "alembic downgrade" not in rollback
    assert "up -d --force-recreate --no-deps --no-build --wait" in rollback


def test_candidate_schema_rollback_retains_schema_and_skips_old_migrate(tmp_path):
    candidate = "222222222222"
    result, log, receipt, _, _ = _run_deploy_rollback(tmp_path, candidate)

    assert result.returncode == 0, result.stderr
    assert "alembic downgrade" not in log
    assert "up --force-recreate --no-deps --no-build migrate" not in log
    assert "backend admin-web" in log
    assert receipt == {
        "ROLLBACK_STATUS": "complete",
        "ROLLBACK_SCHEMA_POLICY": "schema-forward",
        "ROLLBACK_CURRENT_ALEMBIC_REVISION": candidate,
        "ROLLBACK_SCHEMA_RETAINED": "true",
        "ROLLBACK_OLD_MIGRATE_RAN": "false",
        "ROLLBACK_APPLICATION_GIT_HEAD": "predeploy-git-head",
    }


def test_unchanged_predeploy_schema_may_run_old_migrate(tmp_path):
    result, log, receipt, predeploy, _ = _run_deploy_rollback(tmp_path, "111111111111")

    assert result.returncode == 0, result.stderr
    assert "alembic downgrade" not in log
    assert "up --force-recreate --no-deps --no-build migrate" in log
    assert receipt["ROLLBACK_CURRENT_ALEMBIC_REVISION"] == predeploy
    assert receipt["ROLLBACK_SCHEMA_RETAINED"] == "false"
    assert receipt["ROLLBACK_OLD_MIGRATE_RAN"] == "true"
    assert receipt["ROLLBACK_STATUS"] == "complete"


def test_rollback_refuses_an_unexpected_schema_revision(tmp_path):
    result, log, receipt, _, _ = _run_deploy_rollback(tmp_path, "333333333333")

    assert result.returncode == 2
    assert "unexpected Alembic revision" in result.stderr
    assert "backend admin-web" not in log
    assert receipt["ROLLBACK_STATUS"] == "refused"
    assert receipt["ROLLBACK_CURRENT_ALEMBIC_REVISION"] == "333333333333"
    assert receipt["ROLLBACK_SCHEMA_RETAINED"] == "unknown"


def test_only_isolated_acceptance_round_trip_may_use_alembic_downgrade():
    deploy = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    rollback = (ROOT / "scripts/deploy-rollback.sh").read_text(encoding="utf-8")
    acceptance = (ROOT / "scripts/test-env.sh").read_text(encoding="utf-8")

    assert "alembic downgrade" not in deploy
    assert "alembic downgrade" not in rollback
    assert 'compose run --rm --no-deps migrate alembic downgrade "$predeploy"' in acceptance
    assert 'TEST_PROJECT="auto-gallery-test-' in acceptance
    assert 'TEST_ROOT="$STATE_ROOT/$TEST_RUN_ID"' in acceptance


def test_verified_mode_retains_manifest_correctness_checks():
    source = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")

    assert 'assert payload.get("result") == "pass"' in source
    assert "acceptance manifest expired" in source
    assert "source changed after acceptance" in source
    assert "docker image inspect" in source
    assert "CANDIDATE_BACKEND_IMAGE_ID" in source
    assert "CANDIDATE_ADMIN_IMAGE_ID" in source


def test_resource_governance_never_controls_host_or_other_projects():
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "backend/app/services").glob("*resource*.py")
    )

    for forbidden in (
        "systemctl",
        "docker stop",
        "docker kill",
        "docker pause",
        "earlyoom",
        "photo_serv",
        "search_serv",
    ):
        assert forbidden not in sources


def test_showcase_runtime_surface_is_removed():
    assert not (ROOT / "backend/app/api/showcase.py").exists()
    assert not (ROOT / "backend/app/schemas/showcase.py").exists()
    assert not (ROOT / "admin-web/src/app/admin/settings/showcase/page.tsx").exists()

    router = (ROOT / "backend/app/api/__init__.py").read_text(encoding="utf-8")
    root_page = (ROOT / "admin-web/src/app/page.tsx").read_text(encoding="utf-8")
    assert "/showcase" not in router
    assert 'redirect("/admin")' in root_page


def test_showcase_preferences_have_an_atomic_slideshow_migration():
    migration = (
        ROOT
        / "backend/alembic/versions/f2a4c6e8b0d1_migrate_showcase_to_slideshow_preferences.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: Union[str, None] = "f0d2e4a6b8c1"' in migration
    assert "preferences - 'showcase'" in migration
    assert "preferences -> 'slideshow'" in migration
    for key in ("slideDwellMs", "slideTransition", "slideLoop", "slideShowMeta"):
        assert key in migration
