import { expect, test } from "@playwright/test";
import { profile } from "./accounts.mjs";

// Fictional tickets. The mock follows the API contract in backend/app/routers/api.py,
// including revisions: a stale If-Match is refused the way the server refuses it.
const cors = {
  "access-control-allow-origin": "http://127.0.0.1:3000",
  "access-control-allow-credentials": "true",
  "access-control-allow-methods": "GET, POST, PATCH, OPTIONS",
  "access-control-allow-headers": "content-type, if-match, x-ticketflow-request",
};
const json = (route, value, status = 200) => route.fulfill({
  status, contentType: "application/json", headers: cors, body: JSON.stringify(value),
});
const PIXEL = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4//8/AwAI/AL+0I0YAAAAAElFTkSuQmCC", "base64");

function ticket(id, fields = {}) {
  return {
    id, batch_id: 5, file_id: 1, source_page: 1, position: id - 101, ticket_number: null, delivery_date: null,
    material_type: null, tonnage: null, supplier: null, job_location: null, flags: [], status: "needs_review",
    tonnage_confirmed: false, error: null, unit_price_override: null, price_snapshot_state: "pending",
    unit_price: null, price_source: null, cost: null, duplicates: [], pricing_notes: ["No rate"], revision: `r${id}-1`,
    ...fields,
  };
}

async function mock(page, tickets, { role = "owner", queue = [], intake = "manual" } = {}) {
  const log = { patched: [], verified: [], flagged: [], ifMatch: [] };
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return json(route, profile(role));
    if (path === "/api/status") return json(route, { extraction_configured: false, model: "none" });
    if (path === "/api/projects") return json(route, [{ id: 1, code: "24-115", name: "Fictional Road Job", job_location: null, description: null, status: "active" }]);
    if (path === "/api/suppliers" || path === "/api/materials") return json(route, []);
    if (path === "/api/batches/queue") return json(route, queue);
    if (path === "/api/batches/5") return json(route, { id: 5, project_id: 1, layout: "grid_2x2", intake_mode: intake, status: "ready_for_review", created_at: "2026-10-15T11:00:00Z", files: [{ id: 1, original_name: "fictional.pdf", status: "ready_for_review", error: null }] });
    if (path === "/api/batches/5/tickets") return json(route, tickets.map((t) => ({ ...t })));
    if (path === "/api/batches/5/manifest") return json(route, { detail: "n/a" }, 404);
    if (/\/image$/.test(path)) return route.fulfill({ contentType: "image/png", headers: cors, body: PIXEL });
    const match = path.match(/^\/api\/tickets\/(\d+)(?:\/(verify|flag|skip))?$/);
    if (match) {
      const t = tickets.find((x) => x.id === Number(match[1]));
      const given = request.headers()["if-match"];
      log.ifMatch.push(given);
      if (given && given !== t.revision) {
        return json(route, { detail: { code: "ticket_changed", message: "Someone else changed this ticket while you had it open. Nothing was saved." } }, 409);
      }
      const bump = () => { t.revision = `r${t.id}-${Number(t.revision.split("-")[1]) + 1}`; };
      if (method === "PATCH") {
        Object.assign(t, request.postDataJSON());
        bump();
        log.patched.push({ id: t.id, body: request.postDataJSON() });
        return json(route, { ...t });
      }
      if (match[2] === "verify") {
        t.status = "verified";
        bump();
        log.verified.push(t.id);
        return json(route, { ...t });
      }
      if (match[2] === "flag") {
        t.status = "flagged";
        t.flag_note = request.postDataJSON()?.note ?? null;
        bump();
        log.flagged.push({ id: t.id, note: t.flag_note });
        return json(route, { ...t });
      }
    }
    return json(route, { detail: "Unmocked " + path }, 404);
  });
  return log;
}

const QUEUE = [
  { id: 5, project_id: 1, project_code: "24-115", project_name: "Fictional Road Job", status: "ready_for_review", intake_mode: "manual", created_at: "2026-10-15T11:00:00Z", created_by: "Fictional Clerk", files: 1, failed_files: 0, expected_ticket_count: null, tickets: 8, needs_review: 3, flagged: 1, verified: 4, skipped: 0, failed: 0, faint_slots: 0 },
  { id: 6, project_id: 1, project_code: "24-115", project_name: "Fictional Road Job", status: "failed", intake_mode: "manual", created_at: "2026-10-16T11:00:00Z", created_by: null, files: 2, failed_files: 1, expected_ticket_count: null, tickets: 0, needs_review: 0, flagged: 0, verified: 0, skipped: 0, failed: 0, faint_slots: 0 },
];

test("the intake page shows where work is waiting, without remembering a link", async ({ page }) => {
  await mock(page, [ticket(101)], { queue: QUEUE });
  await page.goto("/upload");
  const row = page.getByRole("row").filter({ hasText: "#5" });
  await expect(row).toContainText("3 to review");
  await expect(row).toContainText("4 of 8");
  await expect(row).toContainText("Fictional Clerk");
  await expect(page.getByRole("row").filter({ hasText: "#6" })).toContainText("1 file(s) failed: retry");
  await expect(row.getByRole("link", { name: "Open batch 5" })).toHaveAttribute("href", "/upload?batch=5");
});

test("opening a batch lands on the first open ticket and keeps the place across a reload", async ({ page }) => {
  await mock(page, [ticket(101, { status: "verified", ticket_number: "1" }), ticket(102), ticket(103)]);
  await page.goto("/upload?batch=5");
  await expect(page.getByText("Ticket 2 of 3")).toBeVisible();
  await expect(page).toHaveURL(/batch=5&ticket=102/);
  await page.keyboard.press("Escape");
  await page.keyboard.press("j");
  await expect(page.getByText("Ticket 3 of 3")).toBeVisible();
  await expect(page).toHaveURL(/ticket=103/);
  await page.reload();
  await expect(page.getByText("Ticket 3 of 3")).toBeVisible();
});

test("a ticket can be entered and accepted from the keyboard alone", async ({ page }) => {
  const log = await mock(page, [ticket(101), ticket(102)]);
  await page.goto("/upload?batch=5&ticket=101");
  // The cursor starts in the first empty field, and Enter walks down the form.
  await expect(page.getByLabel("Ticket number")).toBeFocused();
  await page.keyboard.type("010482");
  await page.keyboard.press("Enter");
  await expect(page.getByLabel("Delivery date")).toBeFocused();
  await page.getByLabel("Delivery date").fill("2026-10-15");
  await page.keyboard.press("Enter");
  await page.keyboard.type("No. 53 Stone");
  await page.keyboard.press("Enter");
  await page.keyboard.type("24.5");
  await page.keyboard.press("Enter");
  await page.keyboard.type("Example Aggregates");
  await page.keyboard.press("Enter");
  await page.keyboard.type("Lot A");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "Accept & next" })).toBeFocused();
  await page.keyboard.press("Control+Enter");
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  expect(log.verified).toEqual([101]);
  expect(log.patched[0].body.ticket_number).toBe("010482");
  // The next ticket is ready to type into straight away.
  await expect(page.getByLabel("Ticket number")).toBeFocused();
});

test("same-as-previous only fills the form; nothing is saved until a person accepts", async ({ page }) => {
  const log = await mock(page, [
    ticket(101, { status: "verified", ticket_number: "1", material_type: "Sand", supplier: "Example Aggregates", job_location: "Lot A", delivery_date: "2026-10-15", tonnage: "20.000" }),
    ticket(102),
  ]);
  await page.goto("/upload?batch=5&ticket=102");
  await expect(page.getByLabel("Material")).toHaveValue("");
  await page.keyboard.press("Alt+KeyD");
  await expect(page.getByLabel("Material")).toHaveValue("Sand");
  await expect(page.getByLabel("Supplier")).toHaveValue("Example Aggregates");
  await expect(page.getByLabel("Job location")).toHaveValue("Lot A");
  // Ticket number, tons and date need a look at each scan, so they stay blank.
  await expect(page.getByLabel("Ticket number")).toHaveValue("");
  await expect(page.getByLabel("Tonnage (tons)")).toHaveValue("");
  await expect(page.getByLabel("Delivery date")).toHaveValue("");
  await expect(page.getByRole("status").getByText(/Unsaved changes/)).toBeVisible();
  expect(log.patched).toEqual([]);
  // The date needs its own deliberate click.
  await page.getByRole("button", { name: /Same date as previous ticket/ }).click();
  await expect(page.getByLabel("Delivery date")).toHaveValue("2026-10-15");
  expect(log.patched).toEqual([]);
});

test("same-as-previous never replaces something already typed", async ({ page }) => {
  await mock(page, [ticket(101, { material_type: "Sand", supplier: "Example Aggregates" }), ticket(102)]);
  await page.goto("/upload?batch=5&ticket=102");
  await page.getByLabel("Material").fill("Base Rock");
  await page.keyboard.press("Alt+KeyD");
  await expect(page.getByLabel("Material")).toHaveValue("Base Rock");
  await expect(page.getByLabel("Supplier")).toHaveValue("Example Aggregates");
});

test("a stale approval is refused, the latest values appear, and nothing advances", async ({ page }) => {
  const tickets = [ticket(101, { ticket_number: "1", tonnage: "20.000" }), ticket(102)];
  const log = await mock(page, tickets);
  await page.goto("/upload?batch=5&ticket=101");
  await expect(page.getByLabel("Tonnage (tons)")).toHaveValue("20.000");
  // A teammate corrects the tons while this page is still showing 20.
  tickets[0].tonnage = "22.000";
  tickets[0].revision = "r101-9";
  await page.getByRole("button", { name: "Accept & next" }).click();
  await expect(page.getByTestId("stale-notice")).toContainText("Someone else changed this ticket");
  await expect(page.getByLabel("Tonnage (tons)")).toHaveValue("22.000");
  await expect(page.getByText("Ticket 1 of 2")).toBeVisible();
  expect(log.verified).toEqual([]);
  // Now they've seen it, approving works and uses the new revision.
  await page.getByRole("button", { name: "Accept & next" }).click();
  await expect.poll(() => log.verified).toEqual([101]);
  expect(log.ifMatch.at(-1)).toBe("r101-9");
});

test("a flag note travels with the ticket and shows in the queue", async ({ page }) => {
  const log = await mock(page, [ticket(101, { ticket_number: "1" }), ticket(102)]);
  await page.goto("/upload?batch=5&ticket=101");
  await page.getByLabel("Note if you set this aside (optional)").fill("Tons smudged, ask the driver");
  await page.getByRole("button", { name: "Flag for later" }).click();
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  expect(log.flagged).toEqual([{ id: 101, note: "Tons smudged, ask the driver" }]);
  await page.getByRole("button", { name: "Flagged", exact: true }).click();
  const row = page.getByRole("button", { name: /#1 · 1/ });
  await expect(row).toContainText("Tons smudged, ask the driver");
  await row.click();
  await expect(page.getByTestId("flag-note")).toContainText("Tons smudged");
});

test("zoom and rotation persist between tickets; rotation doesn't overflow its box", async ({ page }) => {
  await mock(page, [ticket(101), ticket(102)]);
  await page.goto("/upload?batch=5&ticket=101");
  await expect(page.getByText("Ticket 1 of 2")).toBeVisible();
  await page.keyboard.press("Escape");
  await page.keyboard.press("+");
  await page.keyboard.press("+");
  await expect(page.getByTestId("zoom-level")).toHaveText("150%");
  await page.keyboard.press("r");
  await page.keyboard.press("j");
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  await expect(page.getByTestId("zoom-level")).toHaveText("150%");
  await page.keyboard.press("Escape");
  await page.keyboard.press("0");
  await expect(page.getByTestId("zoom-level")).toHaveText("100%");
  const image = await page.getByTestId("scan-box").locator("img").boundingBox();
  const box = await page.getByTestId("scan-box").boundingBox();
  expect(image.x).toBeGreaterThanOrEqual(box.x - 1);
  expect(image.x + image.width).toBeLessThanOrEqual(box.x + box.width + 1);
});

test("single-key shortcuts never fire while someone is typing a ticket value", async ({ page }) => {
  await mock(page, [ticket(101), ticket(102)]);
  await page.goto("/upload?batch=5&ticket=101");
  await page.getByLabel("Ticket number").focus();
  await page.keyboard.type("jkr0+-?");
  await expect(page.getByLabel("Ticket number")).toHaveValue("jkr0+-?");
  await expect(page.getByText("Ticket 1 of 2")).toBeVisible();
  await expect(page.getByTestId("zoom-level")).toHaveText("100%");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("alt-down moves on while typing, and still protects unsaved work", async ({ page }) => {
  await mock(page, [ticket(101), ticket(102)]);
  await page.goto("/upload?batch=5&ticket=101");
  await page.getByLabel("Ticket number").fill("10482");
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.keyboard.press("Alt+ArrowDown");
  await expect(page.getByText("Ticket 1 of 2")).toBeVisible();
  await expect(page.getByLabel("Ticket number")).toHaveValue("10482");
});

test("the shortcut list opens from the keyboard and closes with escape", async ({ page }) => {
  await mock(page, [ticket(101, { ticket_number: "1", material_type: "s", supplier: "s", job_location: "j", delivery_date: "2026-10-15", tonnage: "1" })]);
  await page.goto("/upload?batch=5&ticket=101");
  await expect(page.getByText("Ticket 1 of 1")).toBeVisible();
  await page.keyboard.press("?");
  const dialog = page.getByRole("dialog", { name: "Keyboard shortcuts" });
  await expect(dialog).toContainText("Accept (or save) and go to the next ticket");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});

test("a key pressed in the same moment as typing still sees the unsaved work", async ({ page }) => {
  await mock(page, [ticket(101), ticket(102)]);
  await page.goto("/upload?batch=5&ticket=101");
  await expect(page.getByLabel("Ticket number")).toBeFocused();
  let asked = 0;
  page.on("dialog", (dialog) => { asked += 1; dialog.dismiss(); });
  // No time for anything to happen between typing and the key: the worst case
  // for a slow machine, made certain instead of left to chance.
  await page.evaluate(() => {
    const field = document.getElementById("review-ticket_number");
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(field, "10482");
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", altKey: true, bubbles: true, cancelable: true }));
  });
  await expect(page.getByLabel("Ticket number")).toHaveValue("10482");
  await expect(page.getByText("Ticket 1 of 2")).toBeVisible();
  expect(asked).toBe(1);
});

test("keys right after a ticket opens go to shortcuts, not into a field", async ({ page }) => {
  await mock(page, [ticket(101, { status: "verified", ticket_number: "1" }), ticket(102), ticket(103)]);
  await page.goto("/upload?batch=5");
  await expect(page.getByText("Ticket 2 of 3")).toBeVisible();
  await page.keyboard.press("Escape");
  await page.keyboard.press("j");
  await expect(page.getByText("Ticket 3 of 3")).toBeVisible();
  await page.keyboard.press("Escape");
  await page.keyboard.press("k");
  await expect(page.getByText("Ticket 2 of 3")).toBeVisible();
  await expect(page.getByLabel("Ticket number")).toHaveValue("");
});

for (const [platform, leaves] of [["Win32", true], ["Linux x86_64", true], ["MacIntel", false]]) {
  test(`Alt+Left while typing on ${platform} ${leaves ? "moves between tickets instead of leaving the page" : "stays with the cursor"}`, async ({ page }) => {
    await page.addInitScript((value) => Object.defineProperty(navigator, "platform", { get: () => value }), platform);
    await mock(page, [ticket(101), ticket(102)]);
    await page.goto("/upload?batch=5&ticket=102");
    await expect(page.getByLabel("Ticket number")).toBeFocused();
    const prevented = await page.evaluate(() => !document.getElementById("review-ticket_number").dispatchEvent(
      new KeyboardEvent("keydown", { key: "ArrowLeft", altKey: true, bubbles: true, cancelable: true })));
    expect(prevented).toBe(leaves); // A prevented default is what stops the browser going Back.
    await expect(page.getByText(leaves ? "Ticket 1 of 2" : "Ticket 2 of 2")).toBeVisible();
  });
}

test("a possible duplicate needs a deliberate decision, and declining changes nothing", async ({ page }) => {
  const tickets = [ticket(101, { ticket_number: "10482", material_type: "Sand", supplier: "S", job_location: "J", delivery_date: "2026-10-15", tonnage: "20.000" }), ticket(102)];
  const log = await mock(page, tickets);
  const asks = [];
  await page.route("**/api/tickets/101/verify", async (route) => {
    const body = route.request().postDataJSON();
    asks.push(body);
    if (body?.confirm_duplicate) {
      tickets[0].status = "verified";
      return json(route, { ...tickets[0] });
    }
    return json(route, { detail: { code: "possible_duplicate", message: "S ticket 10482 is already approved (batch 3, page 1, ticket 2)." } }, 409);
  });
  await page.goto("/upload?batch=5&ticket=101");
  page.once("dialog", (dialog) => { expect(dialog.message()).toContain("already approved"); dialog.dismiss(); });
  await page.getByRole("button", { name: "Accept & next" }).click();
  await expect.poll(() => asks.length).toBe(1);
  await expect(page.getByText("Ticket 1 of 2")).toBeVisible();
  expect(log.verified).toEqual([]);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Accept & next" }).click();
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  expect(asks.at(-1)).toEqual({ confirm_duplicate: true });
});

test("bad tons are caught before saving, and units are not converted", async ({ page }) => {
  const log = await mock(page, [ticket(101)]);
  await page.goto("/upload?batch=5&ticket=101");
  await page.getByLabel("Tonnage (tons)").fill("48000 lbs");
  await expect(page.getByText("Don't convert pounds or yards here.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Accept & next" })).toBeDisabled();
  expect(log.patched).toEqual([]);
});

test("a finished batch says so and points to the next job", async ({ page }) => {
  await mock(page, [ticket(101, { status: "verified", ticket_number: "1" })]);
  await page.goto("/upload?batch=5");
  const done = page.getByTestId("batch-complete");
  await expect(done).toContainText("All tickets have been resolved.");
  await expect(done.getByRole("link", { name: "Compare an invoice" })).toHaveAttribute("href", "/invoices");
});

test("a scan that can't load says not to approve without it", async ({ page }) => {
  await mock(page, [ticket(101)]);
  await page.route("**/api/tickets/101/image", (route) => route.fulfill({ status: 404, headers: cors, body: "" }));
  await page.goto("/upload?batch=5&ticket=101");
  await expect(page.getByRole("alert").filter({ hasText: "This scan could not be loaded" })).toContainText("Don't approve");
});

test("the review screen fits one viewport at laptop size", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mock(page, [ticket(101), ticket(102)]);
  await page.goto("/upload?batch=5&ticket=101");
  const accept = page.getByRole("button", { name: "Accept & next" });
  await expect(accept).toBeVisible();
  // The scan and the Accept button are on screen together, with no scrolling.
  await expect.poll(async () => (await accept.boundingBox()).y).toBeLessThan(860);
  const button = await accept.boundingBox();
  const scan = await page.getByTestId("scan-box").boundingBox();
  expect(button.y + button.height).toBeLessThanOrEqual(900);
  expect(scan.y).toBeGreaterThan(0);
  expect(scan.y + scan.height).toBeLessThanOrEqual(900);
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
});
