import { writeFileSync } from "node:fs";
import { loadavg } from "node:os";
import { expect, test } from "@playwright/test";
import { complete, mockReview, ticket } from "./f_helpers.mjs";

// 2,000 fictional tickets from a mocked API. These figures say how fast this screen
// renders on the machine running the test, at the load average recorded beside each
// one. They say nothing about a real server, a real network or real scans. Only compare
// timings taken under similar load: the render counts below don't depend on load at all.
const COUNT = 2000;
const FIRST = 1001;
const OPEN_AT = 1600; // Far into the batch, with 1,500 blank tickets behind it.

function batch() {
  return Array.from({ length: COUNT }, (_, index) => {
    const id = FIRST + index;
    const fields = { position: index % 4, source_page: Math.floor(index / 4) + 1 };
    return index < 1500 ? ticket(id, fields) : complete(id, fields);
  });
}

function manifestFor(tickets) {
  const pages = [];
  for (let p = 0; p * 4 < tickets.length; p += 1) {
    pages.push({
      page: p + 1,
      slots: tickets.slice(p * 4, p * 4 + 4).map((t, i) => ({
        id: t.id, position: i, state: "ticket", detection: "content", decision: null, rotation: 0, crop_box: [0, 0, 1, 1],
        crop_sha256: "x", ticket_id: t.id, ticket_number: t.ticket_number, ticket_status: t.status,
      })),
    });
  }
  return {
    batch_id: 5, layout: "grid_2x2", status: "count_not_confirmed", expected_ticket_count: null,
    counts: { files: 1, failed_files: 0, legacy_files: 0, pages: pages.length, slots: tickets.length, records: tickets.length, suspected_blank: 0, empty: 0, confirmed_blank: 0, needs_review: tickets.length, flagged: 0, verified: 0, failed_tickets: 0, skipped: 0 },
    blockers: [], files: [{ id: 1, original_name: "fictional.pdf", sha256: "x", status: "ready_for_review", error: null, legacy: false, pages }],
  };
}

/** Counts how many components and elements React actually rendered, through the hook its devtools use. */
function installRenderCounter() {
  const commits = [];
  window.__commits = commits;
  window.__REACT_DEVTOOLS_GLOBAL_HOOK__ = {
    isDisabled: false, supportsFiber: true, renderers: new Map(), checkDCE() {},
    inject() { return 1; }, onCommitFiberUnmount() {}, onPostCommitFiberRoot() {}, onScheduleFiberRoot() {},
    onCommitFiberRoot(_id, root) {
      let rendered = 0;
      const stack = [root.current];
      while (stack.length) {
        const fiber = stack.pop();
        const old = fiber.alternate;
        if (!old || fiber.memoizedProps !== old.memoizedProps || fiber.memoizedState !== old.memoizedState) rendered += 1;
        // A shared child means React skipped this whole subtree.
        if (fiber.child && (!old || fiber.child !== old.child)) {
          for (let child = fiber.child; child; child = child.sibling) stack.push(child);
        }
      }
      commits.push(rendered);
    },
  };
}
const renderedSince = (page, run) => (async () => {
  await page.evaluate(() => { window.__commits.length = 0; });
  await run();
  await page.waitForTimeout(250);
  return page.evaluate(() => window.__commits.reduce((sum, count) => sum + count, 0));
})();

/**
 * "busy" is how long the event's own work held up the page. "frame" adds the wait
 * for the next painted frame, which at 60 Hz has a floor of a few milliseconds.
 */
function installProbe() {
  const probe = { busy: [], frame: [] };
  window.__probe = probe;
  const mark = () => {
    const start = performance.now();
    const after = (list) => {
      const channel = new MessageChannel();
      channel.port1.onmessage = () => list.push(performance.now() - start);
      channel.port2.postMessage(0);
    };
    after(probe.busy);
    requestAnimationFrame(() => after(probe.frame));
  };
  window.addEventListener("keydown", (event) => { if (window.__probeOn === "key") mark(event); }, true);
  window.addEventListener("beforeinput", (event) => { if (window.__probeOn === "input") mark(event); }, true);
}
async function sampled(page, kind, run) {
  const loadBefore = loadavg()[0];
  await page.evaluate((value) => { window.__probeOn = value; window.__probe.busy = []; window.__probe.frame = []; }, kind);
  await run();
  await page.waitForTimeout(150);
  const samples = await page.evaluate(() => { window.__probeOn = null; return window.__probe; });
  const stat = (list) => {
    const sorted = [...list].sort((a, b) => a - b);
    return { n: sorted.length, median: Math.round(sorted[Math.floor(sorted.length / 2)] * 10) / 10, p95: Math.round(sorted[Math.floor(sorted.length * 0.95)] * 10) / 10 };
  };
  const load = (value) => Math.round(value * 10) / 10;
  return { busy: stat(samples.busy), frame: stat(samples.frame), load1m: [load(loadBefore), load(loadavg()[0])] };
}

async function open(page, tickets) {
  await page.addInitScript(installRenderCounter);
  await page.addInitScript(installProbe);
  await mockReview(page, tickets, { manifest: () => manifestFor(tickets) });
  await page.goto(`/upload?batch=5&ticket=${FIRST + OPEN_AT}`);
  await expect(page.getByText(`Ticket ${(OPEN_AT + 1).toLocaleString()} of 2,000`).or(page.getByText(`Ticket ${OPEN_AT + 1} of ${COUNT}`))).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("button", { name: /#1 · / })).toHaveCount(1);
  await page.waitForTimeout(400);
}

async function measureAll(page, tickets) {
  const results = {};
  const jobLocation = page.getByLabel("Job location");
  await jobLocation.focus();
  await jobLocation.fill("Lot A plus a bit");
  await page.waitForTimeout(50);
  results.typing = await sampled(page, "input", () => page.keyboard.type("abcdefghijklmnopqrstuvwxyz abcdefghijklmnopqrstuvwxyz"));
  await jobLocation.fill(tickets[OPEN_AT].job_location);
  await jobLocation.blur();
  await page.waitForTimeout(50);

  results.nextTicket = await sampled(page, "key", async () => {
    for (let i = 0; i < 30; i += 1) {
      await page.keyboard.press("j");
      await expect(page.getByTestId("zoom-level")).toBeVisible();
    }
  });
  results.zoomKey = await sampled(page, "key", async () => {
    for (let i = 0; i < 8; i += 1) await page.keyboard.press(i % 2 ? "-" : "+");
  });
  const search = page.getByRole("searchbox", { name: "Search tickets in queue" });
  await search.focus();
  results.queueSearch = await sampled(page, "input", async () => {
    for (let i = 0; i < 5; i += 1) {
      await page.keyboard.type("11600");
      for (let k = 0; k < 5; k += 1) await page.keyboard.press("Backspace");
    }
  });
  await search.blur();
  return results;
}

// Timings only report, so they run when asked: TF_MEASURE=1. The render counts below always run.
test("measure: typing and moving through 2,000 tickets, as fast as this machine goes", async ({ page }, info) => {
  test.skip(!process.env.TF_MEASURE, "Set TF_MEASURE=1 to take timings");
  test.setTimeout(180_000);
  const tickets = batch();
  await open(page, tickets);
  const results = { plain: await measureAll(page, tickets) };
  const devtools = await page.context().newCDPSession(page);
  await devtools.send("Emulation.setCPUThrottlingRate", { rate: 4 });
  results.cpuSlowedFourTimes = await measureAll(page, tickets);
  await devtools.send("Emulation.setCPUThrottlingRate", { rate: 1 });
  const text = JSON.stringify(results, null, 1);
  console.log("LARGE-BATCH MEASURE (mocked API, mocked browser test)\n" + text);
  writeFileSync(info.outputPath("measure.json"), text);
  if (process.env.TF_MEASURE_FILE) writeFileSync(process.env.TF_MEASURE_FILE, text);
});

test("what renders per key press, counted by React itself", async ({ page }) => {
  test.setTimeout(120_000);
  const tickets = batch();
  await open(page, tickets);
  const counts = {};
  const jobLocation = page.getByLabel("Job location");
  await jobLocation.focus();
  await page.keyboard.type("x");
  counts.firstKeystroke = await renderedSince(page, () => page.keyboard.type("y"));
  counts.laterKeystroke = await renderedSince(page, () => page.keyboard.type("z"));
  await jobLocation.fill(tickets[OPEN_AT].job_location);
  await jobLocation.blur();
  counts.nextTicket = await renderedSince(page, () => page.keyboard.press("j"));
  counts.zoomKey = await renderedSince(page, () => page.keyboard.press("+"));
  counts.rotateKey = await renderedSince(page, () => page.keyboard.press("r"));
  console.log("RENDER COUNTS " + JSON.stringify(counts));
  // Generous for the form and a few queue rows. Reading all 2,000 rows would be many thousands.
  expect(counts.laterKeystroke).toBeLessThan(200);
  expect(counts.nextTicket).toBeLessThan(800);
  expect(counts.zoomKey).toBeLessThan(60);
  expect(counts.rotateKey).toBeLessThan(60);
});
