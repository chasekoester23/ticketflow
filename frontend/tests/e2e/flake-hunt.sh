#!/usr/bin/env bash
# Run the mocked browser suite many times on a deliberately slow machine, with
# retries off, and name every test that failed even once.
#
#   FLAKE_REPEAT=5 FLAKE_THROTTLE=4 FLAKE_LOAD=6 frontend/tests/e2e/flake-hunt.sh [playwright args]
#
# FLAKE_REPEAT    runs of each test (default 3)
# FLAKE_THROTTLE  browser CPU slowdown; 4 means four times slower (default 1, none)
# FLAKE_LOAD      busy processes competing for the CPU (default 0). Other work shares the
#                 machine, so they stop on their own if this script dies, however it dies,
#                 and after FLAKE_LOAD_SECONDS at the latest (default 3600).
# FLAKE_NICE      niceness for the tests and busy processes alike, so they compete with
#                 each other and yield to anything else on the machine (default 0)
#
# Works on a generated copy of the specs, so nothing in tests/browser changes. Finding a
# flaky test here means fixing its cause, in the test or the app. Never raise a timeout.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
FRONTEND_DIR="$(cd "$HERE/../.." && pwd)"
WORK="$FRONTEND_DIR/tests/.flake-hunt"
REPORT="${FLAKE_REPORT:-$WORK/report.json}"
PIDS=()
RUN=()
if [ "${FLAKE_NICE:-0}" != 0 ]; then RUN=(nice -n "$FLAKE_NICE"); fi
cleanup() { for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null; done; }
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

rm -rf "$WORK"
mkdir -p "$WORK"
cp "$FRONTEND_DIR"/tests/browser/*.mjs "$HERE/throttled-test.mjs" "$WORK"/
sed -i.bak 's#from "@playwright/test"#from "./throttled-test.mjs"#' "$WORK"/*.spec.mjs
rm -f "$WORK"/*.bak
cat >"$WORK/playwright.config.mjs" <<CONFIG
import base from "../playwright.config.mjs";
export default { ...base, testDir: "$WORK", outputDir: "$WORK/results", preserveOutput: "failures-only" };
CONFIG

for ((n = 0; n < ${FLAKE_LOAD:-0}; n++)); do
  # Busy until this script is gone or the time is up, even after a kill -9 here.
  "${RUN[@]+"${RUN[@]}"}" sh -c 'owner=$1; end=$(($(date +%s) + $2)); while kill -0 "$owner" 2>/dev/null && [ "$(date +%s)" -lt "$end" ]; do i=0; while [ $i -lt 20000 ]; do i=$((i + 1)); done; done' busy $$ "${FLAKE_LOAD_SECONDS:-3600}" &
  PIDS+=($!)
done
echo "Busy processes: ${PIDS[*]:-none}"

cd "$FRONTEND_DIR"
PLAYWRIGHT_JSON_OUTPUT_NAME="$REPORT" FLAKE_THROTTLE="${FLAKE_THROTTLE:-1}" "${RUN[@]+"${RUN[@]}"}" npx playwright test \
  --config "$WORK/playwright.config.mjs" --retries=0 --repeat-each "${FLAKE_REPEAT:-3}" --reporter=json,line "$@" >"$WORK/output.txt" 2>&1
code=$?
tail -5 "$WORK/output.txt" | cut -c1-200

REPORT="$REPORT" node --input-type=module -e '
import { readFileSync } from "node:fs";
const report = JSON.parse(readFileSync(process.env.REPORT, "utf8"));
const seen = new Map();
const walk = (suite, file) => {
  for (const spec of suite.specs ?? []) for (const test of spec.tests) for (const result of test.results) {
    const key = `${spec.file ?? file}: ${spec.title}`;
    const row = seen.get(key) ?? { runs: 0, failed: 0, slowest: 0, errors: new Set() };
    row.runs += 1;
    row.slowest = Math.max(row.slowest, result.duration);
    if (result.status !== "passed" && result.status !== "skipped") {
      row.failed += 1;
      row.errors.add((result.error?.message ?? result.status).replace(/\u001b\[[0-9;]*m/g, "").split("\n").slice(0, 3).join(" ").slice(0, 200));
    }
    seen.set(key, row);
  }
  for (const child of suite.suites ?? []) walk(child, suite.file ?? file);
};
for (const suite of report.suites ?? []) walk(suite, suite.file);
const rows = [...seen.entries()];
const bad = rows.filter(([, row]) => row.failed);
console.log(`${rows.length} tests, ${rows.reduce((n, [, r]) => n + r.runs, 0)} runs, ${bad.length} tests failed at least once.`);
for (const [name, row] of bad) {
  console.log(`FLAKY? ${name}: failed ${row.failed} of ${row.runs}`);
  for (const error of row.errors) console.log(`  ${error}`);
}
console.log("Slowest:");
for (const [name, row] of rows.sort((a, b) => b[1].slowest - a[1].slowest).slice(0, 5)) console.log(`  ${(row.slowest / 1000).toFixed(1)} s ${name}`);
'
exit $code
