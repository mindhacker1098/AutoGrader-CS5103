#!/usr/bin/env bash
set -euo pipefail

# Maintenance commands (e.g. reset-password) run without starting a second worker.
if (( $# )); then
    exec "$@"
fi

if ! docker image inspect "${SANDBOX_IMAGE:-autograder-improved-sandbox:2.0.0}" >/dev/null 2>&1; then
    echo 'The grading sandbox image is missing or Docker is inaccessible.' >&2
    echo 'Load autograder-images.tar and mount /var/run/docker.sock; see RUN-ME.txt.' >&2
    exit 1
fi

mkdir -p "$DATA_DIR"
python -m autograder.cli init-db
python -m autograder.cli bootstrap-admin

worker_pid=''
api_pid=''
cleanup() {
    trap - INT TERM EXIT
    [[ -z "$api_pid" ]] || kill -TERM "$api_pid" 2>/dev/null || true
    [[ -z "$worker_pid" ]] || kill -TERM "$worker_pid" 2>/dev/null || true
    [[ -z "$api_pid" ]] || wait "$api_pid" || true
    [[ -z "$worker_pid" ]] || wait "$worker_pid" || true
}
trap cleanup INT TERM EXIT

python -m autograder.worker --jobs "${WORKER_JOBS:-2}" --sandboxes "${MAX_SANDBOXES:-3}" &
worker_pid=$!
python -m uvicorn autograder.api.main:app --host 0.0.0.0 --port 8020 &
api_pid=$!

# Restart the container if either essential process exits, including a clean exit.
set +e
wait -n "$worker_pid" "$api_pid"
exit_code=$?
set -e
(( exit_code != 0 )) || exit_code=1
exit "$exit_code"
