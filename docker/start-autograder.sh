#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
docker info >/dev/null
docker compose version >/dev/null
if ! docker image inspect autograder-improved:2.0.0 autograder-improved-sandbox:2.0.0 >/dev/null 2>&1; then
    docker load -i autograder-images.tar
fi
docker compose up -d --wait --wait-timeout 120
echo 'Open http://127.0.0.1:8020'
echo 'On first startup, the Administrator account and password are in the logs below.'
echo 'Stop with: docker compose stop'
docker compose logs --tail 60
