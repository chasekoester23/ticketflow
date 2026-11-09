// Whole workflow against the REAL backend, worker and frontend, on a throwaway
// database and fictional documents. No mocks, no AI, no network beyond localhost.
// Run with frontend/tests/e2e/run.sh; it isn't part of the mocked browser suite.
import { expect, test } from "@playwright/test";

const KIT = process.env.DEMO_KIT_DIR;
const API = process.env.NEXT_PUBLIC_API_URL;
const VALUES = (number, tons) => ({
  "Ticket number": number, "Delivery date": "2026-10-15", "Material": "No. 53 Crushed Stone",
  "Tonnage (tons)": tons, "Supplier": "Example Aggregates", "Job location": "North Lot",
});

test("upload, reconcile the scan, approve, export, draft an invoice, and catch the discrepancy", async ({ page, baseURL }) => {
  test.setTimeout(180_000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));

  await page.goto("/login");
  await page.getByLabel(/username/i).fill("demo-owner");
  await page.getByLabel(/^password$/i).fill("demo-password-not-real");
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.waitForURL((url) => !url.pathname.includes("login"));

  const headers = { "X-TicketFlow-Request": "1", Origin: baseURL };
  const post = async (path, data) => {
    const response = await page.request.post(API + path, { headers, data });
    expect(response.ok(), path).toBeTruthy();
    return response.json();
  };
  await post("/api/projects", { code: "24-115", name: "Fictional Road Job" });
  const supplier = await post("/api/suppliers", { name: "Example Aggregates" });
  const material = await post("/api/materials", { name: "No. 53 Crushed Stone" });
  await page.request.put(API + "/api/rates", { headers, data: { supplier_id: supplier.id, material_id: material.id, unit_price: "30" } });

  // 1. Upload a 4-up scan and say how many paper tickets it holds.
  await page.goto("/upload");
  await page.locator("input[type=file]").setInputFiles(`${KIT}/demo-tickets.pdf`);
  await page.getByLabel("Project").selectOption({ index: 1 });
  await page.getByLabel("Paper tickets in this upload (optional)").fill("3");
  await page.getByRole("button", { name: "Queue for manual entry" }).click();

  // 2. The scan check catches the faint ticket before any total looks complete.
  const check = page.getByRole("region", { name: "Scan check" });
  await expect(check.getByText("You expected 3 ticket(s), but 2 are on record.")).toBeVisible({ timeout: 60_000 });
  await expect(check.getByText("Needs attention")).toBeVisible();
  await check.getByRole("article", { name: "Page 1 slot 2" }).getByRole("button", { name: "This is a ticket" }).click();
  await expect(check.getByText("Matches your paper count")).toBeVisible();

  // 3. A person enters and accepts every ticket.
  const workspace = page.getByRole("region", { name: "Ticket review workspace" });
  const entries = { 0: VALUES("10482", "24.50"), 1: VALUES("55120", "18.40"), 3: VALUES("10483", "22.10") };
  for (const position of [0, 1, 3]) {
    await check.getByRole("article", { name: `Page 1 slot ${position + 1}` }).getByRole("button", { name: "Open ticket" }).click();
    for (const [label, value] of Object.entries(entries[position])) {
      await workspace.getByLabel(new RegExp("^" + label.replace(/[()]/g, "\\$&"))).fill(value);
    }
    await workspace.getByRole("button", { name: "Accept & next" }).click();
    await expect(check.getByRole("article", { name: `Page 1 slot ${position + 1}` })).toContainText("verified");
  }

  // 4. Excel export of approved records.
  const exported = await page.request.get(API + "/api/records/export.xlsx");
  expect(exported.status()).toBe(200);
  expect((await exported.body()).subarray(0, 2).toString()).toBe("PK");

  // 5. Invoice PDF becomes a draft beside the original; a manager confirms it.
  await page.goto("/invoices/documents");
  await page.getByLabel("Invoice PDF", { exact: true }).setInputFiles(`${KIT}/demo-invoice.pdf`);
  await page.getByLabel("Project").first().selectOption({ index: 1 });
  await page.getByRole("button", { name: "Upload invoice" }).click();
  const review = page.getByRole("region", { name: "Invoice review" });
  await expect(review.getByLabel("Invoice number")).toHaveValue("EA-2026-1107");
  await expect(review.getByText("Freight 120.00")).toBeVisible();
  await review.getByRole("button", { name: "Confirm line" }).click();
  await expect(review.getByText("Saved as comparison")).toBeVisible();

  // 6. Totals agree, but the wrong ticket number is caught.
  await page.goto("/invoices");
  const line = page.getByRole("row").filter({ hasText: "EA-2026-1107" });
  await expect(line).toContainText("Difference found");
  await line.getByRole("button", { name: "Details" }).click();
  const table = page.getByRole("table", { name: "Ticket-by-ticket check" });
  await expect(table.getByRole("row").filter({ hasText: "55999" })).toContainText("Not in TicketFlow");
  await expect(table.getByRole("row").filter({ hasText: "55120" })).toContainText("Delivered, not on invoice");
  await expect(table.getByRole("row").filter({ hasText: "10482" })).toContainText("Matched");
  await expect(page.getByText("Billed minus verified tons:", { exact: false }).locator("strong")).toHaveText("0.000");

  // 7. Clean matches can be confirmed; the wrong one can't.
  await page.getByRole("button", { name: "Confirm 2 clean matches" }).click();
  await expect(page.getByTestId("evidence-summary")).toContainText("2 of 3 invoice tickets confirmed by a person");

  // 8. The pilot report tells the same story, with links to where to look.
  await page.goto("/reports");
  await expect(page.getByTestId("unresolved")).toBeVisible();
  await expect(page.getByRole("region", { name: "Invoice reconciliation" })).toContainText("Verified tickets linked to an invoice");
  await expect(page.getByRole("region", { name: "Intake" })).toContainText("Physical slots");
  if (process.env.DEMO_SCREENSHOT) await page.screenshot({ path: process.env.DEMO_SCREENSHOT, fullPage: true });

  expect(errors).toEqual([]);
});
