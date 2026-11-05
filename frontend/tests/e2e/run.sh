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
cleanup() { for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done; rm -rf "$WORK"; }
trap cleanup EXIT

# An explicit environment overrides any backend/.env. No key means no AI, by construction.
export DATABASE_URL="sqlite:///$WORK/demo.db" STORAGE_DIR="$WORK/storage"
export EXTRACTION_PROVIDER=gemini GEMINI_API_KEY="" ANTHROPIC_API_KEY="" OPENAI_API_KEY=""
export CORS_ORIGINS="[\"http://127.0.0.1:$WEB_PORT\"]" TRUSTED_HOSTS='["127.0.0.1","localhost"]'
export NEXT_PUBLIC_API_URL="http://127.0.0.1:$API_PORT" DEMO_WEB_URL="http://127.0.0.1:$WEB_PORT"
export DEMO_KIT_DIR="$WORK/kit"

cd "$BACKEND_DIR"
"$PYTHON" -m alembic upgrade head >"$WORK/migrate.log" 2>&1
"$PYTHON" scripts/create_demo_workspace.py
"$PYTHON" scripts/make_demo_kit.py --output-dir "$DEMO_KIT_DIR"
"$PYTHON" -m uvicorn app.main:app --port "$API_PORT" >"$WORK/api.log" 2>&1 & PIDS+=($!)
"$PYTHON" scripts/run_worker.py >"$WORK/worker.log" 2>&1 & PIDS+=($!)

cd "$FRONTEND_DIR"
node node_modules/next/dist/bin/next dev -p "$WEB_PORT" >"$WORK/web.log" 2>&1 & PIDS+=($!)
for _ in $(seq 1 120); do
  curl -sf "http://127.0.0.1:$API_PORT/health" >/dev/null && curl -sf -o /dev/null "http://127.0.0.1:$WEB_PORT/login" && break
  sleep 1
done
CONFIG="$FRONTEND_DIR/tests/e2e/playwright.config.mjs"
# The workflow first, on an empty database. Then the same stack with thousands of records.
CI=1 npx playwright test --config "$CONFIG" --reporter=line demo.spec
(cd "$BACKEND_DIR" && "$PYTHON" scripts/make_synthetic_dataset.py --tickets "${DEMO_TICKETS:-3000}")
CI=1 npx playwright test --config "$CONFIG" --reporter=line scale.spec
