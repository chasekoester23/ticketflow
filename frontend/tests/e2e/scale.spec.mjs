// Two people and thousands of records, against the REAL backend, worker and
// frontend, on a throwaway database. Fictional data with planted mistakes.
// run.sh seeds it (scripts/make_synthetic_dataset.py) before this file runs.
import { expect, test } from "@playwright/test";

const API = process.env.NEXT_PUBLIC_API_URL;

async function signIn(page) {
  await page.goto("/login");
  await page.getByLabel(/username/i).fill("demo-owner");
  await page.getByLabel(/^password$/i).fill("demo-password-not-real");
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.waitForURL((url) => !url.pathname.includes("login"));
}

test("a stale approval is refused, even with two people on the real server", async ({ browser, baseURL }) => {
  const manager = await (await browser.newContext()).newPage();
  const teammate = await (await browser.newContext()).newPage();
  await signIn(manager);
  await signIn(teammate);
  const headers = { "X-TicketFlow-Request": "1", Origin: baseURL };
  const search = await (await manager.request.get(API + "/api/records/search?status=needs_review&q=Example&page_size=1")).json();
  const ticket = search.rows[0];

  await manager.goto(`/upload?batch=${ticket.batch_id}&ticket=${ticket.id}`);
  await expect(manager.getByLabel("Tonnage (tons)")).toHaveValue(ticket.tonnage);

  // A teammate corrects the tons while the manager is still looking at the old value.
  const corrected = await teammate.request.patch(`${API}/api/tickets/${ticket.id}`, { headers, data: { tonnage: "99.125" } });
  expect(corrected.ok()).toBeTruthy();

  await manager.getByRole("button", { name: "Accept & next" }).click();
  await expect(manager.getByTestId("stale-notice")).toContainText("Someone else changed this ticket");
  await expect(manager.getByLabel("Tonnage (tons)")).toHaveValue("99.125");
  const after = await (await manager.request.get(`${API}/api/tickets/${ticket.id}/audit`)).json();
  expect(after.some((event) => event.new === "verified")).toBeFalsy();

  // Now they've seen it, approving works and names them.
  await manager.getByRole("button", { name: "Accept & next" }).click();
  await expect.poll(async () => (await (await manager.request.get(`${API}/api/tickets/${ticket.id}/audit`)).json())
    .some((event) => event.new === "verified")).toBeTruthy();
});

test("the register stays usable with thousands of records", async ({ page }) => {
  await signIn(page);
  const started = Date.now();
  await page.goto("/records");
  await expect(page.getByTestId("records-summary")).toContainText(/\d[\d,]* records/);
  const rows = await page.getByRole("row").count();
  expect(rows).toBeLessThanOrEqual(51); // One page, never all of them.
  await page.getByLabel("Search", { exact: true }).fill("100123");
  await expect(page.getByTestId("records-summary")).toContainText("1 records");
  await expect(page).toHaveURL(/q=100123/);
  expect(Date.now() - started).toBeLessThan(30_000);

  const csv = await page.request.get(API + "/api/records/export.csv?q=100123&columns=ticket_number,tons");
  expect(csv.ok()).toBeTruthy();
  expect((await csv.text()).split("\n")[0]).toContain("Ticket Number,Tonnage");
});

test("name cleanup finds the planted misspellings and only changes what someone applies", async ({ page }) => {
  await signIn(page);
  await page.goto("/records/names");
  const typo = page.getByRole("row").filter({ hasText: "Examle Aggregates" });
  await expect(typo).toContainText("Not in your list");
  await expect(typo.getByRole("combobox")).toHaveValue("Example Aggregates");
  const before = await (await page.request.get(API + "/api/records/search?q=Examle&page_size=1")).json();
  expect(before.total).toBeGreaterThan(0);
  await typo.getByRole("button", { name: /Apply to \d+ unapproved/ }).click();
  await expect(page.getByRole("status")).toContainText('Changed "Examle Aggregates" to "Example Aggregates"');
  const after = await (await page.request.get(API + "/api/records/search?q=Examle&page_size=1")).json();
  expect(after.total).toBe(0);
});

test("every planted invoice mistake gets the right result on screen", async ({ page }) => {
  await signIn(page);
  await page.goto("/invoices");
  await expect(page.getByRole("region", { name: "Invoice overview" })).toBeVisible();
  const expected = {
    "PL-clean": "Tickets and totals match", "PL-ticket_never_delivered": "Difference found", "PL-tons_inflated": "Difference found",
    "PL-ticket_billed_twice": "Difference found", "PL-delivery_left_off": "Difference found", "PL-rate_too_high": "Difference found",
    "PL-bad_multiplication": "Difference found", "PL-leading_zeros_stripped": "Difference found", "PL-ticket_from_other_project": "Difference found",
    "PL-same_ticket_on_earlier_invoice": "Difference found", "PL-delivery_still_in_review": "Incomplete review", "PL-totals_only_clean": "Totals match",
  };
  for (const [invoice, result] of Object.entries(expected)) {
    await expect(page.getByRole("row").filter({ hasText: invoice }), invoice).toContainText(result);
  }
  await page.getByRole("row").filter({ hasText: "PL-tons_inflated" }).getByRole("button", { name: "Details" }).click();
  const bridge = page.getByRole("table", { name: "From approved tons to billed tons" });
  await expect(bridge.getByRole("row").filter({ hasText: "Billed tons differ" })).toContainText("+3.000");
  await expect(page.getByRole("note", { name: "Billing patterns to check" })).toContainText("PL-earlier");
});

const PAGES = ["/", "/projects", "/upload", "/records", "/records/names", "/invoices", "/invoices/documents", "/pricing",
  "/reports", "/reports/project", "/team", "/system", "/account"];

test("no page scrolls sideways on a phone, with real data", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  await signIn(page);
  for (const path of PAGES) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, path).toBeLessThanOrEqual(0);
  }
  await context.close();
});

test("the invoice result and actions are on screen at laptop width without sideways scrolling", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 1366, height: 768 } });
  const page = await context.newPage();
  await signIn(page);
  await page.goto("/invoices");
  const row = page.getByRole("row").filter({ hasText: "PL-tons_inflated" });
  await row.scrollIntoViewIfNeeded();
  for (const name of ["Details", "Correct"]) {
    const box = await row.getByRole("button", { name }).boundingBox();
    expect(box.x + box.width, name).toBeLessThanOrEqual(1366);
  }
  const wrap = page.locator(".tf-table-wrap").filter({ has: row });
  expect(await wrap.evaluate((el) => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(0);
  await context.close();
});

test("the project report and system health load on the real stack", async ({ page }) => {
  await signIn(page);
  await page.goto("/reports/project");
  await expect(page.getByRole("region", { name: "Project totals" })).toContainText("Approved tons");
  await page.goto("/system");
  const worker = page.getByRole("row").filter({ hasText: "Processing worker" });
  await expect(worker).toContainText("OK"); // The run script started a real worker.
  await expect(page.getByRole("row").filter({ hasText: "Database version" })).toContainText("Up to date");
  await expect(page.getByRole("row").filter({ hasText: "Backups" })).toContainText("No backup found");
});
