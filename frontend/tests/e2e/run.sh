#!/usr/bin/env bash
# Real backend + worker + frontend on a throwaway database and fictional files.
# Never touches backend/ticketflow.db or backend/.env values. Stops only what it starts.
# Usage, from anywhere (Node 22, frontend deps installed, Playwright browsers installed):
#   frontend/tests/e2e/run.sh
# Overrides: BACKEND_DIR, PYTHON, FRONTEND_DIR, DEMO_API_PORT, DEMO_WEB_PORT, DEMO_SCREENSHOT
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
FRONTEND_DIR="${FRONTEND_DIR:-$(cd "$HERE/../.." && pwd)}"
BACKEND_DIR="${BACKEND_DIR:-$(cd "$FRONTEND_DIR/../backend" && pwd)}"
PYTHON="${PYTHON:-$BACKEND_DIR/.venv/bin/python}"
API_PORT="${DEMO_API_PORT:-8200}"
WEB_PORT="${DEMO_WEB_PORT:-3200}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ticketflow-demo.XXXXXX")"
PIDS=()
STEP="starting"
LOG=""
# On CI, a failure names the step and quotes its log in an annotation, which anyone can
# read without access to the run's logs. Playwright's own failures are named by its
# github reporter as well.
annotate() {
  [ "${GITHUB_ACTIONS:-}" = "true" ] || return 0
  local body="Failed while $STEP (exit $1)."
  for file in $LOG api.log worker.log web.log; do
    [ -s "$WORK/$file" ] && body+=$'\n'"--- $file:"$'\n'"$(tail -n 25 "$WORK/$file")"
  done
  body="$(printf '%s\n' "$body" | awk 'BEGIN { ORS = "%0A" } { gsub(/%/, "%25"); gsub(/\r/, "%0D"); print }')"
  echo "::error title=Real-stack run failed while $STEP::${body:0:3800}"
}
cleanup() {
  local code=$?
  [ "$code" -eq 0 ] || annotate "$code"
  for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
  rm -rf "$WORK"
}
trap cleanup EXIT
step() { STEP="$1"; LOG="${2:-}"; echo "== $STEP"; }

# An explicit environment overrides any backend/.env. No key means no AI, by construction.
export DATABASE_URL="sqlite:///$WORK/demo.db" STORAGE_DIR="$WORK/storage"
export EXTRACTION_PROVIDER=gemini GEMINI_API_KEY="" ANTHROPIC_API_KEY="" OPENAI_API_KEY=""
export CORS_ORIGINS="[\"http://127.0.0.1:$WEB_PORT\"]" TRUSTED_HOSTS='["127.0.0.1","localhost"]'
export NEXT_PUBLIC_API_URL="http://127.0.0.1:$API_PORT" DEMO_WEB_URL="http://127.0.0.1:$WEB_PORT"
export DEMO_KIT_DIR="$WORK/kit"

cd "$BACKEND_DIR"
step "migrating the throwaway database" migrate.log
"$PYTHON" -m alembic upgrade head >"$WORK/migrate.log" 2>&1
step "creating the demo workspace" setup.log
"$PYTHON" scripts/create_demo_workspace.py >"$WORK/setup.log" 2>&1
"$PYTHON" scripts/make_demo_kit.py --output-dir "$DEMO_KIT_DIR" >>"$WORK/setup.log" 2>&1
cat "$WORK/setup.log"
step "starting the API and worker"
"$PYTHON" -m uvicorn app.main:app --port "$API_PORT" >"$WORK/api.log" 2>&1 & PIDS+=($!)
"$PYTHON" scripts/run_worker.py >"$WORK/worker.log" 2>&1 & PIDS+=($!)

cd "$FRONTEND_DIR"
# The production build, because that's what people will use.
step "building the frontend" build.log
node node_modules/next/dist/bin/next build >"$WORK/build.log" 2>&1 || { tail -40 "$WORK/build.log"; exit 1; }
step "starting the frontend" web.log
node node_modules/next/dist/bin/next start -p "$WEB_PORT" >"$WORK/web.log" 2>&1 & PIDS+=($!)
ready=""
for _ in $(seq 1 120); do
  curl -sf "http://127.0.0.1:$API_PORT/health" >/dev/null && curl -sf -o /dev/null "http://127.0.0.1:$WEB_PORT/login" && { ready=1; break; }
  sleep 1
done
[ -n "$ready" ] || { echo "The API or the frontend never answered."; exit 1; }
CONFIG="$FRONTEND_DIR/tests/e2e/playwright.config.mjs"
# A --reporter on the command line replaces the config's, so ask for github here too.
REPORTER="${GITHUB_ACTIONS:+github,}line"
# The workflow first, on an empty database. Then the same stack with thousands of records.
step "running demo.spec, the whole workflow on an empty database"
CI=1 npx playwright test --config "$CONFIG" --reporter="$REPORTER" demo.spec
step "adding thousands of fictional records" dataset.log
(cd "$BACKEND_DIR" && "$PYTHON" scripts/make_synthetic_dataset.py --tickets "${DEMO_TICKETS:-3000}") >"$WORK/dataset.log" 2>&1
cat "$WORK/dataset.log"
step "running scale.spec, two people and thousands of records"
CI=1 npx playwright test --config "$CONFIG" --reporter="$REPORTER" scale.spec
step "finished"
