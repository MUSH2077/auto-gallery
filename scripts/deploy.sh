#!/usr/bin/env bash
# auto-gallery deploy: immutable local/verified candidates, rollback point,
# serial recreation, and project-local runtime verification.
# Usage: ./scripts/deploy.sh [--verified <acceptance.json>] [--core-only]
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$PROJECT_ROOT"
umask 077

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

DEPLOYMENT_ID="$(date '+%Y%m%d-%H%M%S')"
ROLLBACK_ROOT="${DEPLOY_SNAPSHOT_ROOT:-$(dirname "$PROJECT_ROOT")/auto-gallery-deployments}"
ROLLBACK_DIR="$ROLLBACK_ROOT/$DEPLOYMENT_ID"
ROLLBACK_READY=0
BACKUP_READY=0
DEPLOY_MUTATION_STARTED=0
PREDEPLOY_REVISION=""
CANDIDATE_REVISION=""
ACCEPTANCE_MANIFEST="${ACCEPTANCE_MANIFEST:-}"
DEPLOY_MODE="local"
CORE_ONLY=0
CANDIDATE_SOURCE_DIGEST=""
CANDIDATE_BACKEND_IMAGE=""
CANDIDATE_ADMIN_IMAGE=""
GOVERNED_PROFILES="download_network,import_db,image_derive,video_derive,search_index,maintenance"

usage() {
    cat <<'EOF'
Usage: scripts/deploy.sh [--verified <acceptance.json>] [--core-only]

Default mode builds immutable images for the current source tree and deploys
them without consulting acceptance state. --verified reuses an accepted image
pair and refuses failed, expired, or mismatched evidence. --core-only leaves
all background workers stopped after the foreground stack is healthy.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --verified)
            [[ $# -ge 2 ]] || { echo "--verified requires a manifest path" >&2; exit 2; }
            DEPLOY_MODE="verified"
            ACCEPTANCE_MANIFEST="$2"
            shift 2
            ;;
        --core-only)
            CORE_ONLY=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            echo "Unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

compose() {
    docker compose "$@"
}

record_host_sample() {
    local target="$1"
    python3 - "$target" <<'PY'
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

payload = {"sampled_at": datetime.now(timezone.utc).isoformat(), "observational": True}
try:
    values = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, raw = line.split(":", 1)
        values[key] = int(raw.strip().split()[0]) * 1024
    payload["memory"] = {
        "total_bytes": values.get("MemTotal"),
        "available_bytes": values.get("MemAvailable"),
        "swap_total_bytes": values.get("SwapTotal"),
        "swap_free_bytes": values.get("SwapFree"),
    }
except Exception as exc:
    payload["memory_error"] = type(exc).__name__
for name in ("memory", "io"):
    try:
        full = next(line for line in Path(f"/proc/pressure/{name}").read_text().splitlines() if line.startswith("full "))
        payload[f"{name}_psi_full"] = dict(item.split("=", 1) for item in full.split()[1:])
    except Exception as exc:
        payload[f"{name}_psi_error"] = type(exc).__name__
Path(sys.argv[1]).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

check_backup_capacity() {
    local available_kib database_bytes=0 required_bytes postgres_id
    available_kib="$(df -Pk "$ROLLBACK_ROOT" | awk 'NR == 2 {print $4}')"
    [[ "$available_kib" =~ ^[0-9]+$ ]] || {
        echo "Cannot determine free space for rollback root: $ROLLBACK_ROOT" >&2
        return 1
    }
    postgres_id="$(compose ps -q postgres 2>/dev/null || true)"
    if [[ -n "$postgres_id" ]]; then
        database_bytes="$(compose exec -T postgres sh -c \
            'psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --tuples-only --no-align --command="SELECT pg_database_size(current_database())"' \
            | tr -d '[:space:]')"
        [[ "$database_bytes" =~ ^[0-9]+$ ]] || {
            echo "Cannot estimate the live PostgreSQL backup size" >&2
            return 1
        }
    fi
    # Allow two uncompressed database sizes plus 512 MiB for source, Redis,
    # configuration and checksum files. This is project storage, not a host
    # performance class assumption.
    required_bytes=$((database_bytes * 2 + 512 * 1024 * 1024))
    (( available_kib * 1024 >= required_bytes )) || {
        echo "Insufficient rollback space: need ${required_bytes} bytes under $ROLLBACK_ROOT" >&2
        return 1
    }
}

tag_live_image() {
    local service="$1" rollback_tag="$2" image_id
    local container_id
    container_id="$(compose ps -q "$service")"
    [[ -n "$container_id" ]] || return 1
    image_id="$(docker inspect "$container_id" --format '{{.Image}}')"
    if [[ -z "$image_id" ]]; then
        echo "Cannot resolve the live image for $service" >&2
        return 1
    fi
    docker image tag "$image_id" "$rollback_tag"
    printf '%s\n' "$image_id"
}

existing_source_paths() {
    git ls-files -co --exclude-standard -z \
        | while IFS= read -r -d '' path; do
            # `git ls-files -c` includes tracked paths deleted in the working
            # tree. They represent an intentional absence in this candidate,
            # not a file that sha256sum/tar should try to open.
            if [[ -f "$path" || -L "$path" ]]; then
                printf '%s\0' "$path"
            fi
        done
}

source_digest() {
    existing_source_paths \
        | sort -z \
        | while IFS= read -r -d '' path; do
            printf '%s\0' "$path"
            if [[ -L "$path" ]]; then
                printf 'symlink\0%s\n' "$(readlink -- "$path")"
            else
                sha256sum -- "$path"
            fi
        done \
        | sha256sum | awk '{print $1}'
}

resolve_acceptance_manifest() {
    if [[ -z "$ACCEPTANCE_MANIFEST" ]]; then
        local state_root current_file test_root
        state_root="${TEST_STATE_ROOT:-$(dirname "$PROJECT_ROOT")/auto-gallery-test-runs}"
        current_file="$state_root/current"
        [[ -s "$current_file" ]] || {
            echo "Refusing deploy: no acceptance state found" >&2
            return 1
        }
        test_root="$(awk -F= '$1 == "TEST_ROOT" {print substr($0, index($0, "=") + 1)}' "$current_file")"
        ACCEPTANCE_MANIFEST="$test_root/acceptance.json"
    fi
    [[ -s "$ACCEPTANCE_MANIFEST" ]] || {
        echo "Refusing deploy: acceptance manifest is missing: $ACCEPTANCE_MANIFEST" >&2
        return 1
    }

    local values current_digest backend_id admin_id
    values="$(python3 - "$ACCEPTANCE_MANIFEST" <<'PY'
import hashlib, json, sys, time
payload = json.load(open(sys.argv[1], encoding="utf-8"))
claimed_hash = payload.pop("manifest_sha256", None)
canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
assert claimed_hash == hashlib.sha256(canonical).hexdigest(), "acceptance manifest hash mismatch"
assert payload.get("schema_version") == 2, "unsupported acceptance manifest schema"
assert payload.get("result") == "pass", "acceptance result is not pass"
assert float(payload.get("valid_until") or 0) >= time.time(), "acceptance manifest expired"
assert payload.get("deployment_scope") in {"core", "full"}, "unsupported deployment scope"
assert (payload.get("gitllery") or {}).get("product_version") == "v1"
assert (payload.get("gitllery") or {}).get("projection_mode") == "shadow"
requirements = payload.get("requirements") or {}
assert requirements, "acceptance requirements are missing"
assert all((requirements.get("phases") or {}).values()), "required phase did not pass"
assert all((requirements.get("evidence") or {}).values()), "required evidence is incomplete"
print(payload["source_digest"])
print(payload["images"]["backend"])
print(payload["images"]["admin_web"])
PY
)" || return 1
    CANDIDATE_SOURCE_DIGEST="$(sed -n '1p' <<<"$values")"
    backend_id="$(sed -n '2p' <<<"$values")"
    admin_id="$(sed -n '3p' <<<"$values")"
    current_digest="$(source_digest)"
    [[ "$current_digest" == "$CANDIDATE_SOURCE_DIGEST" ]] || {
        echo "Refusing deploy: source changed after acceptance" >&2
        echo "  accepted=$CANDIDATE_SOURCE_DIGEST current=$current_digest" >&2
        return 1
    }
    CANDIDATE_BACKEND_IMAGE="auto-gallery-backend:candidate-$CANDIDATE_SOURCE_DIGEST"
    CANDIDATE_ADMIN_IMAGE="auto-gallery-admin-web:candidate-$CANDIDATE_SOURCE_DIGEST"
    [[ "$(docker image inspect "$CANDIDATE_BACKEND_IMAGE" --format '{{.Id}}')" == "$backend_id" ]]
    [[ "$(docker image inspect "$CANDIDATE_ADMIN_IMAGE" --format '{{.Id}}')" == "$admin_id" ]]
    export BACKEND_IMAGE="$CANDIDATE_BACKEND_IMAGE"
    export ADMIN_IMAGE="$CANDIDATE_ADMIN_IMAGE"
    export RESOURCE_GOVERNANCE_MODE=enforce
    export RESOURCE_GOVERNANCE_MAX_SCALE=1.0
    export RESOURCE_GOVERNANCE_ENFORCED_PROFILES="$GOVERNED_PROFILES"
    export GITLLERY_PROJECTION_MODE=shadow
    export DOWNLOAD_CONCURRENCY_CAP=1
    CANDIDATE_REVISION="$(compose run --rm --no-deps migrate alembic heads | awk '{print $1}' | tail -n 1)"
    [[ "$CANDIDATE_REVISION" =~ ^[a-f0-9]{12}$ ]]
    echo "Accepted candidate: source=$CANDIDATE_SOURCE_DIGEST revision=$CANDIDATE_REVISION"
}

build_local_candidate() {
    local builder_name build_status
    CANDIDATE_SOURCE_DIGEST="$(source_digest)"
    CANDIDATE_BACKEND_IMAGE="auto-gallery-backend:candidate-$CANDIDATE_SOURCE_DIGEST"
    CANDIDATE_ADMIN_IMAGE="auto-gallery-admin-web:candidate-$CANDIDATE_SOURCE_DIGEST"
    export BACKEND_IMAGE="$CANDIDATE_BACKEND_IMAGE"
    export ADMIN_IMAGE="$CANDIDATE_ADMIN_IMAGE"
    export RESOURCE_GOVERNANCE_MODE=enforce
    export RESOURCE_GOVERNANCE_MAX_SCALE=1.0
    export RESOURCE_GOVERNANCE_ENFORCED_PROFILES="$GOVERNED_PROFILES"
    export GITLLERY_PROJECTION_MODE=shadow
    export DOWNLOAD_CONCURRENCY_CAP=1

    echo "Building immutable local candidates for source $CANDIDATE_SOURCE_DIGEST"
    builder_name="auto-gallery-local-${CANDIDATE_SOURCE_DIGEST:0:12}-$$"
    # A docker-container BuildKit worker gives local builds their own cgroup.
    # memory-swap == memory prevents this project build from consuming host
    # swap. Older/minimal Docker installations may lack buildx; those devices
    # retain the serialized low-priority fallback instead of being rejected.
    if [[ "${LOCAL_BUILDX_DISABLE:-0}" != "1" ]] \
        && docker buildx version >/dev/null 2>&1 && docker buildx create \
        --name "$builder_name" \
        --driver docker-container \
        --driver-opt "memory=${LOCAL_BUILD_MEMORY_LIMIT:-1024m}" \
        --driver-opt "memory-swap=${LOCAL_BUILD_MEMORY_LIMIT:-1024m}" \
        --driver-opt "cpu-period=100000" \
        --driver-opt "cpu-quota=${LOCAL_BUILD_CPU_QUOTA:-50000}" \
        >/dev/null; then
        build_status=0
        if command -v nice >/dev/null && command -v ionice >/dev/null; then
            if nice -n 10 ionice -c 2 -n 7 docker buildx bake \
                --builder "$builder_name" --load -f docker-compose.yaml backend \
                && nice -n 10 ionice -c 2 -n 7 docker buildx bake \
                --builder "$builder_name" --load -f docker-compose.yaml admin-web; then
                :
            else
                build_status=$?
            fi
        elif command -v nice >/dev/null; then
            if nice -n 10 docker buildx bake \
                --builder "$builder_name" --load -f docker-compose.yaml backend \
                && nice -n 10 docker buildx bake \
                --builder "$builder_name" --load -f docker-compose.yaml admin-web; then
                :
            else
                build_status=$?
            fi
        else
            if docker buildx bake --builder "$builder_name" --load \
                -f docker-compose.yaml backend \
                && docker buildx bake --builder "$builder_name" --load \
                -f docker-compose.yaml admin-web; then
                :
            else
                build_status=$?
            fi
        fi
        docker buildx rm "$builder_name" >/dev/null 2>&1 || true
        [[ "$build_status" -eq 0 ]] || return "$build_status"
    elif command -v nice >/dev/null && command -v ionice >/dev/null; then
        echo "Bounded BuildKit driver unavailable; using serialized low-priority build" >&2
        COMPOSE_PARALLEL_LIMIT=1 nice -n 10 ionice -c 2 -n 7 docker compose build backend admin-web
    elif command -v nice >/dev/null; then
        echo "Bounded BuildKit driver unavailable; using serialized CPU-low-priority build" >&2
        COMPOSE_PARALLEL_LIMIT=1 nice -n 10 docker compose build backend admin-web
    else
        echo "Bounded BuildKit driver unavailable; using serialized portable build" >&2
        COMPOSE_PARALLEL_LIMIT=1 docker compose build backend admin-web
    fi
    docker image inspect "$CANDIDATE_BACKEND_IMAGE" "$CANDIDATE_ADMIN_IMAGE" >/dev/null
    CANDIDATE_REVISION="$(compose run --rm --no-deps migrate alembic heads | awk '{print $1}' | tail -n 1)"
    [[ "$CANDIDATE_REVISION" =~ ^[a-f0-9]{12}$ ]]
    [[ "$(compose run --rm --no-deps migrate alembic heads | wc -l)" -eq 1 ]]
    echo "Local candidate: source=$CANDIDATE_SOURCE_DIGEST revision=$CANDIDATE_REVISION"
}

persist_runtime_env() {
    python3 - <<'PY'
from pathlib import Path
import os
import tempfile

path = Path(".env")
updates = {
    "RESOURCE_GOVERNANCE_MODE": "enforce",
    "RESOURCE_GOVERNANCE_MAX_SCALE": "1.0",
    "RESOURCE_GOVERNANCE_ENFORCED_PROFILES": "download_network,import_db,image_derive,video_derive,search_index,maintenance",
    "GITLLERY_PROJECTION_MODE": "shadow",
    "DOWNLOAD_CONCURRENCY_CAP": "1",
}
seen: set[str] = set()
lines: list[str] = []
for line in path.read_text(encoding="utf-8").splitlines():
    key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else None
    if key in updates:
        lines.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        lines.append(line)
for key, value in updates.items():
    if key not in seen:
        lines.append(f"{key}={value}")
fd, temporary = tempfile.mkstemp(prefix=".env.core-rollout.", dir=".")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

prepare_rollback_point() {
    local backend_id admin_id candidate_backend_id candidate_admin_id

    install -d -m 700 "$ROLLBACK_DIR"
    cp docker-compose.yaml "$ROLLBACK_DIR/docker-compose.candidate.yaml"
    chmod 600 "$ROLLBACK_DIR/docker-compose.candidate.yaml"
    if git show HEAD:docker-compose.yaml >"$ROLLBACK_DIR/docker-compose.predeploy.yaml"; then
        chmod 600 "$ROLLBACK_DIR/docker-compose.predeploy.yaml"
    else
        echo "Unable to snapshot the pre-deploy Compose file from Git HEAD" >&2
        return 1
    fi
    if [[ -f .env ]]; then
        cp .env "$ROLLBACK_DIR/.env.predeploy"
        chmod 600 "$ROLLBACK_DIR/.env.predeploy"
    fi

    git rev-parse HEAD >"$ROLLBACK_DIR/git-head.txt"
    git status --short >"$ROLLBACK_DIR/git-status.txt"
    git diff --binary HEAD >"$ROLLBACK_DIR/worktree.patch"
    git ls-files --others --exclude-standard >"$ROLLBACK_DIR/untracked-files.txt"
    existing_source_paths | \
        tar --null --files-from=- -czf "$ROLLBACK_DIR/source-tree.tar.gz"
    compose ps -a --format json >"$ROLLBACK_DIR/compose-ps.before.jsonl"
    if [[ "$DEPLOY_MODE" == "verified" && -s "$ACCEPTANCE_MANIFEST" ]]; then
        cp "$ACCEPTANCE_MANIFEST" "$ROLLBACK_DIR/acceptance.json"
    else
        printf '%s\n' "$DEPLOY_MODE" >"$ROLLBACK_DIR/deployment-mode.txt"
    fi

    backend_id="$(tag_live_image backend "auto-gallery-backend:rollback-$DEPLOYMENT_ID")"
    admin_id="$(tag_live_image admin-web "auto-gallery-admin-web:rollback-$DEPLOYMENT_ID")"
    candidate_backend_id="$(docker image inspect "$CANDIDATE_BACKEND_IMAGE" --format '{{.Id}}')"
    candidate_admin_id="$(docker image inspect "$CANDIDATE_ADMIN_IMAGE" --format '{{.Id}}')"
    PREDEPLOY_REVISION="$(compose exec -T postgres sh -c \
        'psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --tuples-only --no-align --command="SELECT version_num FROM alembic_version"')"
    PREDEPLOY_REVISION="${PREDEPLOY_REVISION//[[:space:]]/}"
    [[ "$PREDEPLOY_REVISION" =~ ^[a-f0-9]{12}$ ]]

    cat >"$ROLLBACK_DIR/manifest.env" <<EOF
DEPLOYMENT_ID=$DEPLOYMENT_ID
PROJECT_ROOT=$PROJECT_ROOT
PREDEPLOY_GIT_HEAD=$(git rev-parse HEAD)
PREDEPLOY_ALEMBIC_REVISION=$PREDEPLOY_REVISION
CANDIDATE_ALEMBIC_REVISION=$CANDIDATE_REVISION
CANDIDATE_SOURCE_DIGEST=$CANDIDATE_SOURCE_DIGEST
BACKEND_IMAGE_ID=$backend_id
BACKEND_ROLLBACK_TAG=auto-gallery-backend:rollback-$DEPLOYMENT_ID
ADMIN_IMAGE_ID=$admin_id
ADMIN_ROLLBACK_TAG=auto-gallery-admin-web:rollback-$DEPLOYMENT_ID
CANDIDATE_BACKEND_IMAGE=$CANDIDATE_BACKEND_IMAGE
CANDIDATE_BACKEND_IMAGE_ID=$candidate_backend_id
CANDIDATE_ADMIN_IMAGE=$CANDIDATE_ADMIN_IMAGE
CANDIDATE_ADMIN_IMAGE_ID=$candidate_admin_id
EOF
    chmod 600 "$ROLLBACK_DIR/manifest.env"

    cat >"$ROLLBACK_DIR/rollback.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$PROJECT_ROOT"

# Preserve the new resource ceilings during rollback. Heavy workers stay
# stopped; restore browsing first, then investigate before resuming work.
docker compose --project-directory "$PROJECT_ROOT" -p auto-gallery \
  --env-file "$ROLLBACK_DIR/.env.predeploy" \
  -f "$ROLLBACK_DIR/docker-compose.candidate.yaml" \
  stop -t 120 migrate backend admin-web worker-download worker-import worker-operations scheduler || true

current_revision="\$(docker compose --project-directory "$PROJECT_ROOT" -p auto-gallery \
  --env-file "$ROLLBACK_DIR/.env.predeploy" \
  -f "$ROLLBACK_DIR/docker-compose.candidate.yaml" \
  exec -T postgres sh -c \
  'psql --username="\$POSTGRES_USER" --dbname="\$POSTGRES_DB" --tuples-only --no-align --command="SELECT version_num FROM alembic_version"')"
current_revision="\${current_revision//[[:space:]]/}"
case "\$current_revision" in
  "$PREDEPLOY_REVISION")
    ;;
  "$CANDIDATE_REVISION")
    docker image inspect "$CANDIDATE_BACKEND_IMAGE" >/dev/null
    docker image tag "$CANDIDATE_BACKEND_IMAGE" auto-gallery-backend:latest
    docker compose --project-directory "$PROJECT_ROOT" -p auto-gallery \
      --env-file "$ROLLBACK_DIR/.env.predeploy" \
      -f "$ROLLBACK_DIR/docker-compose.candidate.yaml" \
      run --rm --no-deps migrate alembic downgrade $PREDEPLOY_REVISION
    ;;
  *)
    echo "Refusing rollback from unexpected Alembic revision: \$current_revision" >&2
    exit 2
    ;;
esac

docker image tag "auto-gallery-backend:rollback-$DEPLOYMENT_ID" auto-gallery-backend:latest
docker image tag "auto-gallery-admin-web:rollback-$DEPLOYMENT_ID" auto-gallery-admin-web:latest
docker compose --project-directory "$PROJECT_ROOT" -p auto-gallery \
  --env-file "$ROLLBACK_DIR/.env.predeploy" \
  -f "$ROLLBACK_DIR/docker-compose.candidate.yaml" \
  up -d --force-recreate --no-build --wait --wait-timeout 180 \
  postgres redis meilisearch migrate backend admin-web
docker compose --project-directory "$PROJECT_ROOT" -p auto-gallery \
  --env-file "$ROLLBACK_DIR/.env.predeploy" \
  -f "$ROLLBACK_DIR/docker-compose.candidate.yaml" ps
docker compose --project-directory "$PROJECT_ROOT" -p auto-gallery \
  --env-file "$ROLLBACK_DIR/.env.predeploy" \
  -f "$ROLLBACK_DIR/docker-compose.candidate.yaml" \
  exec -T backend curl -sf http://localhost:8000/api/v1/system/ready >/dev/null
EOF
    chmod 700 "$ROLLBACK_DIR/rollback.sh"

    ROLLBACK_READY=1
    echo -e "${GREEN}  Images and rollback procedure: $ROLLBACK_DIR${NC}"
}

backup_frozen_state() {
    local redis_id

    echo "Creating a consistent PostgreSQL backup..."
    compose exec -T postgres sh -c \
        'exec pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom --compress=3' \
        >"$ROLLBACK_DIR/postgres.dump.tmp"
    mv "$ROLLBACK_DIR/postgres.dump.tmp" "$ROLLBACK_DIR/postgres.dump"
    [[ -s "$ROLLBACK_DIR/postgres.dump" ]]
    compose exec -T postgres pg_restore --list \
        <"$ROLLBACK_DIR/postgres.dump" >"$ROLLBACK_DIR/postgres.restore-list.txt"
    [[ -s "$ROLLBACK_DIR/postgres.restore-list.txt" ]]

    echo "Saving and stopping Redis before copying its complete persistent state..."
    redis_id="$(compose ps -q redis)"
    [[ -n "$redis_id" ]]
    compose exec -T redis sh -c \
        'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --no-auth-warning SAVE >/dev/null'
    compose stop -t 30 redis
    install -d -m 700 "$ROLLBACK_DIR/redis-data"
    docker cp "$redis_id:/data/." "$ROLLBACK_DIR/redis-data"
    [[ -s "$ROLLBACK_DIR/redis-data/dump.rdb" ]]

    if [[ -d data/config ]]; then
        tar -C "$PROJECT_ROOT" -czf "$ROLLBACK_DIR/app-config.tar.gz" data/config
    fi
    install -d -m 700 "$ROLLBACK_DIR/sqlite"
    if [[ -d data/downloads ]]; then
        while IFS= read -r -d '' sqlite_file; do
            cp -p "$sqlite_file" "$ROLLBACK_DIR/sqlite/"
        done < <(find data/downloads -maxdepth 1 -type f \
            \( -name 'archive-*.sqlite3*' -o -name '.file-index.sqlite3*' \) -print0)
    fi

    cat >"$ROLLBACK_DIR/README.txt" <<'EOF'
This directory is a deployment rollback point. Run rollback.sh for an ordinary
code/migration rollback; it does not overwrite PostgreSQL or Redis data.

Use postgres.dump only after confirmed data corruption and a separate restore
review. To restore Redis after confirmed data loss, first stop Redis and move
the current /data contents (especially appendonlydir) aside, then restore the
entire redis-data directory. Redis append-only mode loads AOF in preference to
dump.rdb, so copying only dump.rdb beside an old AOF does not restore this point.
EOF

    chmod -R u+rwX,go-rwx "$ROLLBACK_DIR"
    (
        cd "$ROLLBACK_DIR"
        while IFS= read -r -d '' file; do
            sha256sum "$file"
        done < <(find . -type f ! -name SHA256SUMS -print0 | sort -z)
    ) >"$ROLLBACK_DIR/SHA256SUMS"
    chmod 600 "$ROLLBACK_DIR/SHA256SUMS"
    (cd "$ROLLBACK_DIR" && sha256sum --check SHA256SUMS)
    touch "$ROLLBACK_DIR/snapshot.complete"
    chmod 600 "$ROLLBACK_DIR/snapshot.complete"
    BACKUP_READY=1
    echo -e "${GREEN}  Complete rollback point: $ROLLBACK_DIR${NC}"
}

all_services_ready() {
    local service container_id state health
    local services=("$@")

    if [[ ${#services[@]} -eq 0 ]]; then
        services=(postgres redis meilisearch backend admin-web)
    fi

    for service in "${services[@]}"; do
        container_id="$(compose ps -q "$service")"
        [[ -n "$container_id" ]] || return 1
        state="$(docker inspect "$container_id" --format '{{.State.Status}}')"
        health="$(docker inspect "$container_id" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')"
        [[ "$state" == "running" ]] || return 1
        [[ "$health" == "healthy" ]] || return 1
    done
}

deploy_failed() {
    local status=$?
    trap - ERR
    echo -e "${RED}Deployment did not complete successfully.${NC}" >&2
    if [[ "$DEPLOY_MUTATION_STARTED" -eq 1 ]]; then
        compose stop -t 60 migrate worker-download worker-import worker-operations scheduler >/dev/null 2>&1 || true
    fi
    if [[ "$DEPLOY_MUTATION_STARTED" -eq 1 && "$ROLLBACK_READY" -eq 1 && -x "$ROLLBACK_DIR/rollback.sh" ]]; then
        echo "Attempting fail-closed foreground rollback; heavy workers stay stopped..." >&2
        if "$ROLLBACK_DIR/rollback.sh" >&2; then
            echo "Automatic foreground rollback completed." >&2
        else
            echo "Automatic rollback could not complete." >&2
            echo "Rollback assets are preserved at: $ROLLBACK_DIR" >&2
            echo "After review, retry: $ROLLBACK_DIR/rollback.sh" >&2
        fi
    fi
    echo "Background workers may intentionally remain stopped; inspect 'docker compose ps' before any retry." >&2
    compose ps >&2 || true
    exit "$status"
}

trap deploy_failed ERR

echo -e "${GREEN}=== auto-gallery Safe Deploy ===${NC}"

if [[ ! -f .env ]]; then
    echo "Refusing deploy: .env is missing" >&2
    false
fi
env_mode="$(stat -c '%a' .env)"
if (( (8#$env_mode & 8#022) != 0 )); then
    echo "Refusing deploy: .env permissions are $env_mode; run 'chmod 600 .env' first" >&2
    false
fi
command -v docker >/dev/null
docker info >/dev/null
compose config --quiet
install -d -m 700 "$ROLLBACK_ROOT"
[[ -w "$ROLLBACK_ROOT" ]] || {
    echo "Refusing deploy: rollback root is not writable: $ROLLBACK_ROOT" >&2
    false
}
check_backup_capacity

# ── 1. Candidate ──────────────────────────────────────────────────────
if [[ "$DEPLOY_MODE" == "verified" ]]; then
    echo -e "${YELLOW}[1/7] Verifying the accepted immutable candidate...${NC}"
    resolve_acceptance_manifest
else
    echo -e "${YELLOW}[1/7] Building an immutable local candidate...${NC}"
    build_local_candidate
fi

# ── 2. Validate ───────────────────────────────────────────────────────
echo -e "${YELLOW}[2/7] Validating the Compose resource contract...${NC}"
python3 scripts/verify-compose-resources.py
echo -e "${GREEN}  Resource contract OK${NC}"

# ── 3. Protect live images and freeze heavy writers ──────────────────
echo -e "${YELLOW}[3/7] Protecting live images and freezing background writers...${NC}"
prepare_rollback_point
DEPLOY_MUTATION_STARTED=1
compose stop -t 120 worker-download worker-import worker-operations scheduler
persist_runtime_env

# ── 4. Reuse the exact candidate images ──────────────────────────────
echo -e "${YELLOW}[4/7] Reusing immutable candidate images (no rebuild)...${NC}"
docker image inspect "$CANDIDATE_BACKEND_IMAGE" "$CANDIDATE_ADMIN_IMAGE" >/dev/null

# ── 5. Freeze foreground, back up, migrate and recreate ──────────────
echo -e "${YELLOW}[5/7] Freezing foreground writes and creating checked backups...${NC}"
compose stop -t 120 admin-web backend
backup_frozen_state
[[ "$BACKUP_READY" -eq 1 && -f "$ROLLBACK_DIR/snapshot.complete" ]]
echo -e "${YELLOW}[5/7] Recreating the protected stack one service at a time...${NC}"
COMPOSE_PARALLEL_LIMIT=1 compose up -d --force-recreate \
    postgres redis meilisearch migrate backend admin-web
compose stop -t 60 worker-download worker-import worker-operations scheduler >/dev/null 2>&1 || true

# ── 6. Wait for healthy ───────────────────────────────────────────────
echo -e "${YELLOW}[6/7] Waiting up to 180 seconds for every service...${NC}"
ready=0
for attempt in $(seq 1 36); do
    if all_services_ready; then
        ready=1
        break
    fi
    echo "  Waiting... ($((attempt * 5))s)"
    sleep 5
done
if [[ "$ready" -ne 1 ]]; then
    echo "Timed out waiting for all services to become healthy" >&2
    compose ps >&2
    compose logs --tail=50 backend migrate redis postgres >&2 || true
    false
fi

# ── 7. Project-local verification and background startup ─────────────
echo -e "${YELLOW}[7/7] Verifying project runtime behavior...${NC}"
VERIFY_SCOPE=core VERIFY_ALLOW_CRITICAL_PRESSURE=1 \
    VERIFY_EXPECT_GOVERNANCE_MODE=enforce \
    VERIFY_EXPECT_ENFORCED_PROFILES="$GOVERNED_PROFILES" \
    bash scripts/verify-runtime.sh

if [[ "$CORE_ONLY" -eq 0 ]]; then
    echo -e "${YELLOW}[7/7] Starting adaptive background workers...${NC}"
    compose up -d --force-recreate --no-build \
        worker-download worker-import worker-operations scheduler
    ready=0
    for attempt in $(seq 1 36); do
        if all_services_ready worker-download worker-import worker-operations scheduler; then
            ready=1
            break
        fi
        echo "  Waiting for workers... ($((attempt * 5))s)"
        sleep 5
    done
    [[ "$ready" -eq 1 ]] || {
        compose ps >&2
        compose logs --tail=50 worker-download worker-import worker-operations scheduler >&2 || true
        false
    }
    VERIFY_SCOPE=full VERIFY_ALLOW_CRITICAL_PRESSURE=1 \
        VERIFY_EXPECT_GOVERNANCE_MODE=enforce \
        VERIFY_EXPECT_ENFORCED_PROFILES="$GOVERNED_PROFILES" \
        bash scripts/verify-runtime.sh
fi

record_host_sample "$ROLLBACK_DIR/host-observation.json" \
    || echo "Warning: unable to write optional host observation" >&2
[[ "$(docker inspect "$(compose ps -q backend)" --format '{{.Image}}')" == \
    "$(docker image inspect "$CANDIDATE_BACKEND_IMAGE" --format '{{.Id}}')" ]]
[[ "$(docker inspect "$(compose ps -q admin-web)" --format '{{.Image}}')" == \
    "$(docker image inspect "$CANDIDATE_ADMIN_IMAGE" --format '{{.Id}}')" ]]

# Only after every runtime check passes do future ordinary Compose restarts
# resolve latest to the candidate images. Running containers already use the
# immutable candidate tags exported above.
docker image tag "$CANDIDATE_BACKEND_IMAGE" auto-gallery-backend:latest
docker image tag "$CANDIDATE_ADMIN_IMAGE" auto-gallery-admin-web:latest
cat >"$ROLLBACK_ROOT/active-deployment.env" <<EOF
DEPLOYMENT_ID=$DEPLOYMENT_ID
ROLLBACK_DIR=$ROLLBACK_DIR
ACCEPTANCE_MANIFEST=$([[ "$DEPLOY_MODE" == verified ]] && echo "$ROLLBACK_DIR/acceptance.json" || true)
SOURCE_DIGEST=$CANDIDATE_SOURCE_DIGEST
ALEMBIC_REVISION=$CANDIDATE_REVISION
BACKEND_IMAGE=$CANDIDATE_BACKEND_IMAGE
BACKEND_IMAGE_ID=$(docker image inspect "$CANDIDATE_BACKEND_IMAGE" --format '{{.Id}}')
ADMIN_IMAGE=$CANDIDATE_ADMIN_IMAGE
ADMIN_IMAGE_ID=$(docker image inspect "$CANDIDATE_ADMIN_IMAGE" --format '{{.Id}}')
ROLLOUT_STAGE=adaptive
DEPLOYMENT_MODE=$DEPLOY_MODE
DEPLOYMENT_SCOPE=$([[ "$CORE_ONLY" -eq 1 ]] && echo core || echo full)
SHADOW_STARTED_AT_EPOCH=$(date +%s)
STAGE_CHANGED_AT_EPOCH=$(date +%s)
EOF
chmod 600 "$ROLLBACK_ROOT/active-deployment.env"

trap - ERR
echo ""
echo -e "${GREEN}=== Deploy complete ===${NC}"
echo "  API:       http://localhost:8818"
echo "  Admin:     http://localhost:13000"
echo "  Rollback:  $ROLLBACK_DIR/rollback.sh"
echo "  Monitor:   python3 scripts/monitor-nas-resources.py --interval 10 --duration 86400"
if [[ "$CORE_ONLY" -eq 1 ]]; then
    echo "  Workers:   stopped by --core-only"
else
    echo "  Workers:   running with adaptive project-local governance"
fi
