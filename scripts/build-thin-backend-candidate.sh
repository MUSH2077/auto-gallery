#!/usr/bin/env bash
set -Eeuo pipefail

base_image="${1:?base image required}"
target_image="${2:?target image required}"
source_root="${3:?backend source root required}"
source_digest="${4:?source digest required}"
project="${5:?acceptance project required}"
container="auto-gallery-candidate-builder-${source_digest:0:12}-$$"
project_root="$(dirname "$source_root")"

cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker create --name "$container" --network none \
  --memory 512m --memory-swap 512m --cpus 0.40 --pids-limit 96 \
  --label "com.docker.compose.project=$project" \
  --label "com.auto-gallery.environment=acceptance" \
  "$base_image" sh -c 'exec sleep infinity' >/dev/null
docker start "$container" >/dev/null
docker exec "$container" install -d -m 755 /candidate-app
docker cp "$source_root/." "$container:/candidate-app/"
docker cp "$source_root/gitllery" "$container:/usr/local/bin/gitllery"
docker exec "$container" chmod 755 /usr/local/bin/gitllery
python3 "$project_root/scripts/run-container-pressure-controlled.py" \
  --container "$container" --project "$project" \
  --baseline-file "${TEST_ROOT:?acceptance TEST_ROOT is required}/reports/host-baseline.json" -- \
  docker exec -w /candidate-app -e PYTHONPATH=/candidate-app "$container" \
    sh -c 'python -m pip check && python -c "import gitllery_cli.main, gitllery_format" && gitllery --help >/dev/null'
docker commit \
  --change 'WORKDIR /candidate-app' \
  --change 'ENV PYTHONPATH=/candidate-app' \
  --change 'CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]' \
  --change "LABEL org.opencontainers.image.revision=$source_digest" \
  "$container" "$target_image" >/dev/null
docker image inspect "$target_image" --format '{{.Id}}'
