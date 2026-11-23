#!/usr/bin/env bash
# Review-workflow benchmark: the real API and production frontend on a throwaway
# database, one batch of fictional AI-read tickets, and a scripted keyboard reviewer.
# Never touches backend/ticketflow.db or backend/.env values. Stops only what it starts.
# Usage (Node 22, frontend deps and Playwright installed):
#   frontend/tests/e2e/review-bench.sh [tickets] [output.json]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
FRONTEND_DIR="$(cd "$HERE/../.." && pwd)"
BACKEND_DIR="$(cd "$FRONTEND_DIR/../backend" && pwd)"
PYTHON="${PYTHON:-$BACKEND_DIR/.venv/bin/python}"
TICKETS="${1:-300}"
OUT="$(cd "$(dirname "${2:-review-bench.json}")" && pwd)/$(basename "${2:-review-bench.json}")"
API_PORT="${BENCH_API_PORT:-8210}"
WEB_PORT="${BENCH_WEB_PORT:-3210}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ticketflow-bench.XXXXXX")"
PIDS=()
cleanup() { for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done; rm -rf "$WORK"; }
trap cleanup EXIT

export DATABASE_URL="sqlite:///$WORK/demo.db" STORAGE_DIR="$WORK/storage"
export EXTRACTION_PROVIDER=gemini GEMINI_API_KEY="" ANTHROPIC_API_KEY="" OPENAI_API_KEY=""
export CORS_ORIGINS="[\"http://127.0.0.1:$WEB_PORT\"]" TRUSTED_HOSTS='["127.0.0.1","localhost"]'
export NEXT_PUBLIC_API_URL="http://127.0.0.1:$API_PORT" DEMO_WEB_URL="http://127.0.0.1:$WEB_PORT"
export BENCH_ANSWERS="$WORK/answers.json" BENCH_OUT="$OUT"
# Rate limits are per person per minute; a scripted reviewer would hit them, a person wouldn't.
export RATE_LIMIT_UPLOADS_PER_MINUTE=0 RATE_LIMIT_REPORTS_PER_MINUTE=0 RATE_LIMIT_EXPORTS_PER_MINUTE=0

cd "$BACKEND_DIR"
"$PYTHON" -m alembic upgrade head >"$WORK/migrate.log" 2>&1
"$PYTHON" scripts/create_demo_workspace.py >/dev/null
"$PYTHON" scripts/make_review_benchmark.py --tickets "$TICKETS" --answers "$BENCH_ANSWERS"
"$PYTHON" -m uvicorn app.main:app --port "$API_PORT" >"$WORK/api.log" 2>&1 & PIDS+=($!)

cd "$FRONTEND_DIR"
node node_modules/next/dist/bin/next build >"$WORK/build.log" 2>&1 || { tail -40 "$WORK/build.log"; exit 1; }
node node_modules/next/dist/bin/next start -p "$WEB_PORT" >"$WORK/web.log" 2>&1 & PIDS+=($!)
for _ in $(seq 1 120); do
  curl -sf "http://127.0.0.1:$API_PORT/health" >/dev/null && curl -sf -o /dev/null "http://127.0.0.1:$WEB_PORT/login" && break
  sleep 1
done
CI=1 npx playwright test --config "$FRONTEND_DIR/tests/e2e/playwright.config.mjs" --reporter=line review-bench.spec
echo "Report: $OUT"
