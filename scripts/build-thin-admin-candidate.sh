#!/usr/bin/env bash
set -Eeuo pipefail

source_root="${1:?admin source root required}"
source_digest="${2:?source digest required}"
project="${3:?acceptance project required}"
target_image="${4:?target image required}"
project_root="$(dirname "$source_root")"

node_image='node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293'
lock_digest="$(sha256sum "$source_root/package.json" "$source_root/package-lock.json" | sha256sum | awk '{print $1}')"
build_root="/var/tmp/auto-gallery-admin-build-root"
build_root_marker="$build_root/.auto-gallery-admin-build-root"
install -d -m 700 "$build_root" "$build_root/runs"
install -d -m 755 "$build_root/runs/docs"
if [[ ! -f "$build_root_marker" ]]; then
  printf '%s\n' 'auto-gallery-admin-build-root-v1' >"$build_root_marker"
fi
[[ "$(cat "$build_root_marker")" == 'auto-gallery-admin-build-root-v1' ]] || {
  echo "Frontend build root marker mismatch: $build_root" >&2
  exit 2
}
dependency_cache="$build_root/deps-${lock_digest:0:16}"
builder="auto-gallery-admin-builder-${source_digest:0:12}-$$"
runtime="auto-gallery-admin-runtime-${source_digest:0:12}-$$"
docker_root="$(docker info --format '{{.DockerRootDir}}')"
block_device="$(readlink -f "$(findmnt -n -o SOURCE -T "$docker_root")")"
local_mount_device="$(readlink -f "$(findmnt -n -o SOURCE -T /overlay)")"
local_parent="$(lsblk -n -o PKNAME "$local_mount_device" | head -1)"
local_block_device="${local_parent:+/dev/$local_parent}"
[[ -b "$local_block_device" ]] || { echo "Cannot resolve local build block device" >&2; exit 2; }
scratch="$(mktemp -d -p "$build_root/runs" run.XXXXXXXX)"
scratch_name="$(basename "$scratch")"
container_scratch="/build-root/runs/$scratch_name"
container_cache="/build-root/$(basename "$dependency_cache")"
marker="$scratch/.auto-gallery-admin-build"
printf '%s\n' "$source_digest" >"$marker"
chmod 700 "$scratch"
if [[ ! -d "$dependency_cache" ]]; then
  install -d -m 700 "$dependency_cache"
  printf '%s\n' "$lock_digest" >"$dependency_cache/.auto-gallery-admin-deps"
fi
[[ -f "$dependency_cache/.auto-gallery-admin-deps" ]]
[[ "$(cat "$dependency_cache/.auto-gallery-admin-deps")" == "$lock_digest" ]] || {
  echo "Frontend dependency cache marker mismatch: $dependency_cache" >&2
  exit 2
}

cleanup() {
  docker unpause "$builder" >/dev/null 2>&1 || true
  docker rm -f "$builder" "$runtime" >/dev/null 2>&1 || true
  if [[ "$scratch" == "$build_root"/runs/run.* ]] \
    && [[ -f "$marker" ]] \
    && [[ "$(cat "$marker")" == "$source_digest" ]]; then
    docker run --rm --network none --mount "type=bind,src=$build_root,dst=/build-root" \
      --entrypoint sh "$node_image" -c \
      'scratch="/build-root/runs/$2"; marker="$scratch/.auto-gallery-admin-build"; test "$(cat "$marker")" = "$1" && find "$scratch" -mindepth 1 -maxdepth 1 ! -name .auto-gallery-admin-build -exec rm -rf -- {} + && rm -f "$marker"' \
      sh "$source_digest" "$scratch_name" >/dev/null 2>&1 || true
  fi
  if [[ "$scratch" == "$build_root"/runs/run.* ]] \
    && [[ -d "$scratch" ]] \
    && [[ -z "$(find "$scratch" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
    rmdir "$scratch" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

limits=(
  --memory 896m --memory-swap 896m --cpus 0.40 --pids-limit 128
  --device-read-bps "$block_device:8mb" --device-write-bps "$block_device:8mb"
  --device-read-iops "$block_device:250" --device-write-iops "$block_device:250"
  --device-read-bps "$local_block_device:4mb" --device-write-bps "$local_block_device:2mb"
  --device-read-iops "$local_block_device:50" --device-write-iops "$local_block_device:50"
  --label "com.docker.compose.project=$project"
  --label 'com.auto-gallery.environment=acceptance'
)

docker create --name "$builder" --network bridge "${limits[@]}" \
  --mount "type=bind,src=$build_root,dst=/build-root" \
  --mount "type=bind,src=$source_root,dst=/source,readonly" \
  --mount "type=bind,src=$project_root/docs,dst=/build-root/runs/docs,readonly" \
  --entrypoint sh "$node_image" -c 'exec sleep infinity' >/dev/null
docker start "$builder" >/dev/null

python3 "$project_root/scripts/run-container-pressure-controlled.py" \
  --container "$builder" --project "$project" \
  --baseline-file "${TEST_ROOT:?acceptance TEST_ROOT is required}/reports/host-baseline.json" --resume-samples 8 -- \
  docker exec "$builder" sh -c \
    'tar --exclude=.next --exclude=node_modules --exclude=.git -C /source -cf - . | tar -C "$1" -xf -' \
    sh "$container_scratch"

python3 "$project_root/scripts/run-container-pressure-controlled.py" \
  --container "$builder" --project "$project" \
  --baseline-file "$TEST_ROOT/reports/host-baseline.json" --resume-samples 8 -- \
  docker exec -w "$container_scratch" \
    -e npm_config_cache="$container_cache/npm" \
    -e npm_config_maxsockets=2 -e UV_THREADPOOL_SIZE=1 \
    "$builder" sh -c '
      cache="$2"
      work="$3"
      if test -f "$cache/node_modules.ready" && test "$(cat "$cache/node_modules.ready")" = "$1"; then
        # Keep the cache immutable while presenting a real directory to Next.
        # A symlink here is copied verbatim into .next/standalone and points to
        # /build-root after the runtime image no longer has that build mount.
        cp -al "$cache/node_modules" "$work/node_modules"
      else
        set -e
        npm ci
        rm -rf "$cache/node_modules.pending" "$cache/node_modules"
        cp -al "$work/node_modules" "$cache/node_modules.pending"
        mv "$cache/node_modules.pending" "$cache/node_modules"
        printf "%s\n" "$1" > "$cache/node_modules.ready"
      fi
      mkdir -p "$cache/next-$4" "$work/.next"
      ln -s "$cache/next-$4" "$work/.next/cache"
    ' sh "$lock_digest" "$container_cache" "$container_scratch" "$source_digest"
docker network disconnect bridge "$builder"

python3 "$project_root/scripts/run-container-pressure-controlled.py" \
  --container "$builder" --project "$project" \
  --baseline-file "$TEST_ROOT/reports/host-baseline.json" --resume-samples 8 -- \
  docker exec -w "$container_scratch" \
    -e NEXT_TELEMETRY_DISABLED=1 -e NODE_OPTIONS=--max-old-space-size=640 \
    "$builder" sh -c '
    npm run prepare:api-docs &&
    npm run check:i18n &&
    npm run check:charts &&
    npm run check:media &&
    npm run check:search-contract &&
    npm run check:admin-routes &&
    npm run check:api-types &&
    npm run typecheck &&
    npm run build -- --webpack
  '

docker create --name "$runtime" --network none "${limits[@]}" \
  --entrypoint sh "$node_image" -c 'exec sleep infinity' >/dev/null
docker start "$runtime" >/dev/null
docker exec "$runtime" install -d -m 755 /app/.next
python3 "$project_root/scripts/run-container-pressure-controlled.py" \
  --container "$builder" --project "$project" \
  --baseline-file "$TEST_ROOT/reports/host-baseline.json" --resume-samples 8 -- \
  sh -c 'docker exec "$1" tar -C "$3/.next/standalone" -cf - . | docker cp - "$2:/app"' \
    sh "$builder" "$runtime" "$container_scratch"
python3 "$project_root/scripts/run-container-pressure-controlled.py" \
  --container "$builder" --project "$project" \
  --baseline-file "$TEST_ROOT/reports/host-baseline.json" --resume-samples 8 -- \
  sh -c 'docker exec "$1" tar -C "$3/.next" -cf - static | docker cp - "$2:/app/.next"' \
    sh "$builder" "$runtime" "$container_scratch"
python3 "$project_root/scripts/run-container-pressure-controlled.py" \
  --container "$builder" --project "$project" \
  --baseline-file "$TEST_ROOT/reports/host-baseline.json" --resume-samples 8 -- \
  sh -c 'docker exec "$1" tar -C "$3" -cf - public entrypoint.sh | docker cp - "$2:/app"' \
    sh "$builder" "$runtime" "$container_scratch"
docker exec "$runtime" chmod 755 /app/entrypoint.sh

docker commit \
  --change 'WORKDIR /app' \
  --change 'ENV NODE_ENV=production' \
  --change 'ENTRYPOINT ["/app/entrypoint.sh"]' \
  --change "LABEL org.opencontainers.image.revision=$source_digest" \
  "$runtime" "$target_image" >/dev/null
docker image inspect "$target_image" --format '{{.Id}}'
