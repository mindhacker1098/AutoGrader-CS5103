#!/usr/bin/env bash
# Start API (:8000), worker and the Vite dev server (:5173). Ctrl+C stops all three.
#   scripts/run_dev.sh [--seed] [--no-frontend]
# Settings come from .env / the environment (DATA_DIR, SANDBOX, LLM_CACHE_DIR, ...).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe; else PY=.venv/bin/python; fi
export PYTHONPATH="$ROOT/backend${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1

SEED=0
FRONTEND=1
for a in "$@"; do
  case "$a" in
    --seed) SEED=1 ;;
    --no-frontend) FRONTEND=0 ;;
    *) echo "unknown option $a" >&2; exit 2 ;;
  esac
done

"$PY" -m autograder.cli init-db
[ "$SEED" = 1 ] && "$PY" -m autograder.cli seed-demo

pids=()
WORKER_ID="dev-$(hostname)-$$"
stop_tree() {
  # on Windows the venv python and npm are launchers; kill the whole tree
  local win
  if win=$(cat "/proc/$1/winpid" 2>/dev/null); then
    taskkill //PID "$win" //T //F >/dev/null 2>&1 || true
  else
    kill -INT "$1" 2>/dev/null || true
    sleep 2
    kill "$1" 2>/dev/null || true
  fi
}
cleanup() {
  trap - INT TERM EXIT
  for p in "${pids[@]}"; do stop_tree "$p"; done
  docker ps -aq --filter "label=autograder.worker=$WORKER_ID" 2>/dev/null | xargs -r docker rm -f >/dev/null 2>&1 || true
}
trap cleanup INT TERM EXIT

"$PY" -m uvicorn autograder.api.main:app --port "${API_PORT:-8000}" &
pids+=($!)
"$PY" -m autograder.worker --jobs "${WORKER_JOBS:-2}" --sandboxes "${MAX_SANDBOXES:-3}" --worker-id "$WORKER_ID" &
pids+=($!)
if [ "$FRONTEND" = 1 ]; then
  [ -d frontend/node_modules ] || (cd frontend && npm install)
  (cd frontend && exec npm run dev) &
  pids+=($!)
fi

echo "api http://localhost:${API_PORT:-8000}/api/health   ui http://localhost:5173   (Ctrl+C to stop)"
wait -n || true
