#!/usr/bin/env bash
set -euo pipefail

docker_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
compose=(docker compose -f "$docker_dir/docker-compose.yml" -f "$docker_dir/docker-compose-sidecar.yml")
health_url="${OPENBOXES_HEALTH_URL:-http://localhost/openboxes/health}"

if ! command -v docker >/dev/null || ! command -v curl >/dev/null; then
  echo "Docker and curl are required." >&2
  exit 1
fi

if [[ -z "$("${compose[@]}" ps --status running -q app)" ]]; then
  echo "The local OpenBoxes app is not running. Start the Compose stack before using this restart script." >&2
  exit 1
fi

echo "Restarting the local OpenBoxes app container..."
"${compose[@]}" restart app

echo "Waiting for $health_url to report UP..."
deadline=$((SECONDS + 180))
while (( SECONDS < deadline )); do
  if response="$(curl --fail --silent --max-time 5 "$health_url")" &&
      grep -Eq '"status"[[:space:]]*:[[:space:]]*"UP"' <<< "$response"; then
    echo "OpenBoxes is healthy (HTTP 200, status UP)."
    exit 0
  fi
  sleep 2
done

echo "OpenBoxes did not become healthy within 180 seconds." >&2
"${compose[@]}" ps app >&2
exit 1
