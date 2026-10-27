#!/usr/bin/env bash
set -Eeuo pipefail

source_root="${1:?backend source root required}"
target_image="${2:?production image required}"
test_image="${3:?test image required}"
revision="${4:?revision required}"
project="${5:?acceptance project required}"

build_args=(--label "com.docker.compose.project=$project"
  --label "com.auto-gallery.environment=acceptance"
  --build-arg "BUILD_REVISION=$revision")
for name in PYTHON_BASE PIP_INDEX_URL HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy; do
  if [[ -v "$name" ]]; then
    build_args+=(--build-arg "$name=${!name}")
  fi
done

docker build "${build_args[@]}" --target production -t "$target_image" "$source_root"
docker build "${build_args[@]}" --target test -t "$test_image" "$source_root"
