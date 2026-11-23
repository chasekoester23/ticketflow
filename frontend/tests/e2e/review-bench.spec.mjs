// A scripted reviewer works through one batch on the REAL backend and frontend
// (tests/e2e/review-bench.sh seeds it), keyboard only, correcting exactly the planted
// misreads, and records what the software costs a person per ticket:
//   - wait: from pressing Accept to the next ticket's scan and form being ready
//   - keystrokes it took, and API requests made
// It never measures a person reading or deciding: that time is zero here, so these
// figures are the software's share of the work, not human throughput.
import { readFileSync, writeFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

const API = process.env.NEXT_PUBLIC_API_URL;
const answers = JSON.parse(readFileSync(process.env.BENCH_ANSWERS, "utf8"));
// Optional pause per ticket standing in for a person reading the scan, so requests that
// only happen at human pace (debounced reloads) are counted. Excluded from wait_ms.
const THINK_MS = Number(process.env.BENCH_THINK_MS ?? 0);
const percentile = (values, p) => { const s = [...values].sort((a, b) => a - b); return s.length ? s[Math.min(s.length - 1, Math.floor(p * s.length))] : null; };

async function signIn(page) {
  await page.goto("/login");
  await page.getByLabel(/username/i).fill("demo-owner");
  await page.getByLabel(/^password$/i).fill("demo-password-not-real");
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.waitForURL((url) => !url.pathname.includes("login"));
}

/** The ticket on screen, once its scan has loaded and its form is up; null until then. */
const shown = (previous) => {
  const id = Number(new URLSearchParams(window.location.search).get("ticket"));
  if (!id || id === previous) return null;
  const image = document.querySelector('[data-testid="scan-box"] img');
  if (!image || !image.src.includes(`/tickets/${id}/image`) || !image.complete || !image.naturalWidth) return null;
  return document.getElementById("review-ticket_number") ? id : null;
};

test("review benchmark", async ({ page }) => {
  test.setTimeout(60 * 60_000);
  await signIn(page);
  const requests = [];
  page.on("request", (request) => { if (request.url().startsWith(API)) requests.push(new URL(request.url()).pathname.replace(/\d+/g, ":id")); });

  const started = Date.now();
  await page.goto(`/upload?batch=${answers.batch_id}`);
  let current = await (await page.waitForFunction(shown, 0, { timeout: 60_000 })).jsonValue();
  const firstReady = Date.now() - started;
  const tickets = [];
  let keystrokes = 0;
  for (let n = 0; n < answers.tickets; n++) {
    if (THINK_MS) await page.waitForTimeout(THINK_MS);
    const fixes = answers.corrections[String(current)];
    expect(fixes, `ticket ${current} should be one of the benchmark's`).toBeDefined();
    const before = requests.length;
    let keys = 0;
    const correcting = Date.now();
    for (const [field, value] of Object.entries(fixes)) {
      await page.focus(`#review-${field}`); keys += 1;           // a Tab or a click to reach it
      await page.keyboard.press("ControlOrMeta+a"); keys += 1;
      await page.keyboard.type(value); keys += value.length;
    }
    const corrected = Date.now() - correcting;
    const accepted = Date.now();
    await page.keyboard.press("Control+Enter"); keys += 1;
    keystrokes += keys;
    if (n === answers.tickets - 1) {
      await expect.poll(async () => (await (await page.request.get(`${API}/api/batches/${answers.batch_id}/tickets`)).json())
        .filter((t) => t.status === "verified").length, { timeout: 30_000 }).toBe(answers.tickets);
      tickets.push({ id: current, corrected_fields: Object.keys(fixes).length, correcting_ms: corrected, wait_ms: Date.now() - accepted,
                     keys, requests: requests.length - before });
      break;
    }
    const next = await (await page.waitForFunction(shown, current, { timeout: 30_000 })).jsonValue();
    tickets.push({ id: current, corrected_fields: Object.keys(fixes).length, correcting_ms: corrected, wait_ms: Date.now() - accepted,
                   keys, requests: requests.length - before });
    current = next;
  }
  const total = Date.now() - started;
  const waits = tickets.map((t) => t.wait_ms);
  const counted = {};
  requests.forEach((path) => { counted[path] = (counted[path] ?? 0) + 1; });
  const report = {
    kind: "AUTOMATED: a scripted reviewer, not a person. Software cost only, not human throughput.",
    think_ms_per_ticket: THINK_MS,
    tickets: tickets.length, tickets_corrected: tickets.filter((t) => t.corrected_fields).length,
    first_ticket_ready_ms: firstReady, total_ms: total,
    wait_after_accept_ms: { median: percentile(waits, 0.5), p90: percentile(waits, 0.9), max: Math.max(...waits) },
    keystrokes_per_ticket: +(keystrokes / tickets.length).toFixed(2),
    keystrokes_per_clean_ticket: percentile(tickets.filter((t) => !t.corrected_fields).map((t) => t.keys), 0.5),
    requests_per_ticket: +(requests.length / tickets.length).toFixed(2),
    requests_by_path: Object.fromEntries(Object.entries(counted).sort((a, b) => b[1] - a[1])),
    software_only_tickets_per_hour: Math.round(tickets.length / (total / 3_600_000)),
  };
  writeFileSync(process.env.BENCH_OUT, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
});
