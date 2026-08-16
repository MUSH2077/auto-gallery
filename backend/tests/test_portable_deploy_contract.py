from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


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
    assert '--driver-opt "memory=${LOCAL_BUILD_MEMORY_LIMIT:-1024m}"' in source
    assert '--driver-opt "memory-swap=${LOCAL_BUILD_MEMORY_LIMIT:-1024m}"' in source
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


def test_source_digest_and_snapshot_skip_tracked_deletions():
    deploy = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    acceptance = (ROOT / "scripts/test-env.sh").read_text(encoding="utf-8")

    for source in (deploy, acceptance):
        assert "existing_source_paths" in source
        assert '[[ -f "$path" || -L "$path" ]]' in source
    assert "existing_source_paths | \\" in deploy
    assert "tar --null --files-from=-" in deploy


def test_project_backup_and_core_health_failures_are_fail_closed():
    source = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")

    assert "check_backup_capacity" in source
    assert "pg_database_size(current_database())" in source
    assert 'if "$ROLLBACK_DIR/rollback.sh"' in source
    assert "Automatic foreground rollback completed" in source
    assert "DEPLOY_MUTATION_STARTED=0" in source
    assert 'if [[ "$DEPLOY_MUTATION_STARTED" -eq 1 ]]' in source


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
