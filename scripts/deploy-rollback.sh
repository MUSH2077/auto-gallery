#!/usr/bin/env bash
# Production application rollback. The database is always treated as
# schema-forward: a candidate schema is retained and never downgraded.
set -Eeuo pipefail

umask 077
ROLLBACK_DIR="$(cd "$(dirname "$0")" && pwd -P)"
# shellcheck source=/dev/null
source "$ROLLBACK_DIR/manifest.env"

required_manifest_values=(
    DEPLOYMENT_ID
    PROJECT_ROOT
    PREDEPLOY_GIT_HEAD
    PREDEPLOY_ALEMBIC_REVISION
    CANDIDATE_ALEMBIC_REVISION
    BACKEND_IMAGE_ID
    BACKEND_ROLLBACK_TAG
    ADMIN_IMAGE_ID
    ADMIN_ROLLBACK_TAG
    ROLLBACK_SCHEMA_POLICY
    ROLLBACK_SCHEMA_CURRENT_REVISION_AT_SNAPSHOT
    ROLLBACK_SCHEMA_RETAIN_CANDIDATE
    ROLLBACK_OLD_MIGRATE_ONLY_AT_PREDEPLOY
)
for name in "${required_manifest_values[@]}"; do
    if [[ -z "${!name:-}" ]]; then
        echo "Rollback manifest is missing $name" >&2
        exit 2
    fi
done
if [[ "$ROLLBACK_SCHEMA_POLICY" != "schema-forward" \
      || "$ROLLBACK_SCHEMA_RETAIN_CANDIDATE" != "true" \
      || "$ROLLBACK_OLD_MIGRATE_ONLY_AT_PREDEPLOY" != "true" \
      || "$ROLLBACK_SCHEMA_CURRENT_REVISION_AT_SNAPSHOT" != "$PREDEPLOY_ALEMBIC_REVISION" ]]; then
    echo "Refusing rollback with an unsupported schema policy" >&2
    exit 2
fi
if [[ ! "$PREDEPLOY_ALEMBIC_REVISION" =~ ^[a-f0-9]{12}$ \
      || ! "$CANDIDATE_ALEMBIC_REVISION" =~ ^[a-f0-9]{12}$ ]]; then
    echo "Refusing rollback with invalid Alembic revisions" >&2
    exit 2
fi

cd "$PROJECT_ROOT"
unset BACKEND_IMAGE ADMIN_IMAGE

rollback_status="checking_schema"
current_revision="unknown"
schema_retained="unknown"
old_migrate_ran="false"

write_receipt() {
    local temporary="$ROLLBACK_DIR/rollback-receipt.env.tmp"
    {
        printf 'ROLLBACK_STATUS=%s\n' "$rollback_status"
        printf 'ROLLBACK_SCHEMA_POLICY=%s\n' "$ROLLBACK_SCHEMA_POLICY"
        printf 'ROLLBACK_CURRENT_ALEMBIC_REVISION=%s\n' "$current_revision"
        printf 'ROLLBACK_SCHEMA_RETAINED=%s\n' "$schema_retained"
        printf 'ROLLBACK_OLD_MIGRATE_RAN=%s\n' "$old_migrate_ran"
        printf 'ROLLBACK_APPLICATION_GIT_HEAD=%s\n' "$PREDEPLOY_GIT_HEAD"
    } >"$temporary"
    chmod 600 "$temporary"
    mv "$temporary" "$ROLLBACK_DIR/rollback-receipt.env"
}

record_failure() {
    local exit_code=$?
    trap - ERR
    rollback_status="failed"
    write_receipt
    exit "$exit_code"
}
trap record_failure ERR
write_receipt

current_revision="$(docker compose --project-directory "$PROJECT_ROOT" -p auto-gallery \
    --env-file "$ROLLBACK_DIR/.env.predeploy" \
    -f "$ROLLBACK_DIR/docker-compose.candidate.yaml" \
    exec -T postgres sh -c \
    'psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --tuples-only --no-align --command="SELECT version_num FROM alembic_version"')"
current_revision="${current_revision//[[:space:]]/}"

case "$current_revision" in
    "$PREDEPLOY_ALEMBIC_REVISION")
        schema_retained="false"
        ;;
    "$CANDIDATE_ALEMBIC_REVISION")
        # The old image may not know the candidate Alembic graph. Retain the
        # additive schema and do not start its migration entrypoint.
        schema_retained="true"
        ;;
    *)
        rollback_status="refused"
        write_receipt
        trap - ERR
        echo "Refusing rollback from unexpected Alembic revision: $current_revision" >&2
        exit 2
        ;;
esac

rollback_status="restoring_application"
write_receipt

# Keep heavy workers stopped. Restore only the foreground application while
# operators establish whether the previous image tolerates the retained schema.
docker compose --project-directory "$PROJECT_ROOT" -p auto-gallery \
    --env-file "$ROLLBACK_DIR/.env.predeploy" \
    -f "$ROLLBACK_DIR/docker-compose.candidate.yaml" \
    stop -t 120 migrate backend admin-web worker-download worker-import worker-operations worker-discovery scheduler || true

docker image tag "$BACKEND_ROLLBACK_TAG" auto-gallery-backend:latest
docker image tag "$ADMIN_ROLLBACK_TAG" auto-gallery-admin-web:latest
BACKEND_IMAGE="$BACKEND_IMAGE_ID" ADMIN_IMAGE="$ADMIN_IMAGE_ID" \
    docker compose --project-directory "$PROJECT_ROOT" -p auto-gallery \
    --env-file "$ROLLBACK_DIR/.env.predeploy" \
    -f "$ROLLBACK_DIR/docker-compose.candidate.yaml" \
    up -d --no-build --wait --wait-timeout 180 postgres redis meilisearch

if [[ "$current_revision" == "$PREDEPLOY_ALEMBIC_REVISION" ]]; then
    BACKEND_IMAGE="$BACKEND_IMAGE_ID" ADMIN_IMAGE="$ADMIN_IMAGE_ID" \
        docker compose --project-directory "$PROJECT_ROOT" -p auto-gallery \
        --env-file "$ROLLBACK_DIR/.env.predeploy" \
        -f "$ROLLBACK_DIR/docker-compose.candidate.yaml" \
        up --force-recreate --no-deps --no-build migrate
    old_migrate_ran="true"
    write_receipt
fi

BACKEND_IMAGE="$BACKEND_IMAGE_ID" ADMIN_IMAGE="$ADMIN_IMAGE_ID" \
    docker compose --project-directory "$PROJECT_ROOT" -p auto-gallery \
    --env-file "$ROLLBACK_DIR/.env.predeploy" \
    -f "$ROLLBACK_DIR/docker-compose.candidate.yaml" \
    up -d --force-recreate --no-deps --no-build --wait --wait-timeout 180 \
    backend admin-web
docker compose --project-directory "$PROJECT_ROOT" -p auto-gallery \
    --env-file "$ROLLBACK_DIR/.env.predeploy" \
    -f "$ROLLBACK_DIR/docker-compose.candidate.yaml" ps
docker compose --project-directory "$PROJECT_ROOT" -p auto-gallery \
    --env-file "$ROLLBACK_DIR/.env.predeploy" \
    -f "$ROLLBACK_DIR/docker-compose.candidate.yaml" \
    exec -T backend curl -sf http://localhost:8000/api/v1/system/ready >/dev/null

rollback_status="complete"
write_receipt
trap - ERR
