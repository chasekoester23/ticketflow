import { expect, test } from "@playwright/test";
import { profile } from "./accounts.mjs";

// Fictional supplier, project, tickets and invoice. Mirrors the API contract
// in backend/app/routers/invoices.py.
const cors = {
  "access-control-allow-origin": "http://127.0.0.1:3000",
  "access-control-allow-credentials": "true",
  "access-control-allow-headers": "content-type, x-ticketflow-request",
  "access-control-allow-methods": "GET, POST, PUT, OPTIONS",
};
const json = (route, value, status = 200) => route.fulfill({
  status, contentType: "application/json", headers: cors, body: JSON.stringify(value),
});

const record = (id, number, tons, extra = {}) => ({
  ticket_id: id, batch_id: 3, ticket_number: number, delivery_date: "2026-10-15",
  supplier: "Example Aggregates", material: "No. 53 Stone", tons, review_status: "verified",
  project: "24-115", ...extra,
});

function line(overrides = {}) {
  return {
    id: 9, project_id: 1, invoice_number: "EA-2026-1107", supplier_name: "Example Aggregates",
    material_name: "No. 53 Stone", date_from: "2026-10-01", date_to: "2026-10-31",
    billed_tons: "46.600", billed_unit_price: "30.0000", billed_subtotal: "1398.00",
    billed_extension: "1398.00", invoice_arithmetic_difference: "0.00",
    verified_tons: "46.600", verified_subtotal: "1398.00", quantity_difference: "0.000",
    subtotal_difference: "0.00", verified_count: 2, pending_count: 0, unclassified_count: 0,
    unpriced_count: 0, rate_mismatch_count: 0, status: "matches", comparison_mode: "totals_only",
    warnings: [], tickets: [], ticket_check: [], ticket_check_counts: {},
    itemized_billed_tons: null, created_at: "2026-11-04", ticket_list: "",
    ...overrides,
  };
}

const ITEMIZED = line({
  status: "discrepancy", comparison_mode: "itemized", itemized_billed_tons: "46.600",
  warnings: ["1 invoice ticket number(s) have no matching delivery record."],
  ticket_check_counts: { matched: 1, not_found: 1, not_on_invoice: 1 },
  ticket_check: [
    { result: "matched", line: 1, invoice_ticket_number: "010482", billed_tons: "24.500",
      ticket: record(11, "010482", "24.500"), candidates: [], verified_tons: "24.500",
      tons_difference: "0.000", flags: [], notes: [] },
    { result: "not_found", line: 2, invoice_ticket_number: "10490", billed_tons: "22.100",
      ticket: null, candidates: [], verified_tons: null, tons_difference: null, flags: [],
      notes: ["No delivery record in TicketFlow has this ticket number."] },
    { result: "not_on_invoice", line: null, invoice_ticket_number: null, billed_tons: null,
      ticket: record(12, "10483", "22.100"), candidates: [], verified_tons: "22.100",
      tons_difference: null, flags: [], notes: [] },
  ],
});

async function mockInvoices(page, role, saved, posted = []) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return json(route, profile(role));
    if (path === "/api/projects") return json(route, [{ id: 1, code: "24-115", name: "Fictional Road Job",
      job_location: null, description: null, status: "active" }]);
    if (path === "/api/suppliers") return json(route, [{ id: 1, name: "Example Aggregates", contact: null }]);
    if (path === "/api/materials") return json(route, [{ id: 2, name: "No. 53 Stone", unit: "ton" }]);
    if (path === "/api/invoice-comparisons" && request.method() === "POST") {
      posted.push(request.postDataJSON());
      saved.splice(0, saved.length, ITEMIZED);
      return json(route, ITEMIZED, 201);
    }
    if (path === "/api/invoice-comparisons") return json(route, saved);
    return json(route, { detail: "Not found" }, 404);
  });
}

test("a manager pastes invoice tickets and sees each one checked with links to the scan", async ({ page }) => {
  const posted = [];
  await mockInvoices(page, "manager", [], posted);
  await page.goto("/invoices");
  await page.getByLabel("Project").selectOption("1");
  await page.getByLabel("Invoice number").fill("EA-2026-1107");
  await page.getByLabel("Supplier name (match ticket supplier)").fill("Example Aggregates");
  await page.getByLabel("Material (one material per line)").fill("No. 53 Stone");
  await page.getByLabel("Delivery date from").fill("2026-10-01");
  await page.getByLabel("Delivery date through (inclusive)").fill("2026-10-31");
  await page.getByLabel("Billed quantity (tons)").fill("46.600");
  await page.getByLabel("Billed rate ($ per ton)").fill("30");
  await page.getByLabel("Invoice MATERIAL subtotal ($)").fill("1398.00");
  // Exactly what Excel puts on the clipboard: tab-separated rows.
  await page.getByLabel("Ticket numbers listed on this invoice (optional)")
    .fill("Ticket\tTons\n010482\t24.5\n10490\t22.1");
  await expect(page.getByText("3 rows entered.")).toBeVisible();
  await page.getByRole("button", { name: "Compare invoice line" }).click();

  await expect.poll(() => posted.length).toBe(1);
  expect(posted[0].ticket_list).toBe("Ticket\tTons\n010482\t24.5\n10490\t22.1");

  const table = page.getByRole("table", { name: "Ticket-by-ticket check" });
  await expect(table).toBeVisible();
  const matched = table.getByRole("row").filter({ hasText: "Matched" });
  await expect(matched).toContainText("010482"); // leading zero kept
  await expect(matched.getByRole("link", { name: "Scan" }))
    .toHaveAttribute("href", /\/api\/tickets\/11\/image$/);
  await expect(matched.getByRole("link", { name: "Review & history" }))
    .toHaveAttribute("href", "/upload?batch=3&ticket=11");
  await expect(table.getByRole("row").filter({ hasText: "10490" })).toContainText("Not in TicketFlow");
  const missing = table.getByRole("row").filter({ hasText: "Delivered, not on invoice" });
  await expect(missing).toContainText("10483");
  await expect(missing.getByRole("link", { name: "Review & history" }))
    .toHaveAttribute("href", "/upload?batch=3&ticket=12");
  await expect(page.getByText("Difference found").first()).toBeVisible();
});

test("a totals-only line is labelled as not checking individual tickets", async ({ page }) => {
  await mockInvoices(page, "viewer", [line()]);
  await page.goto("/invoices");
  const row = page.getByRole("row").filter({ hasText: "EA-2026-1107" });
  await expect(row).toContainText("Totals match");
  await expect(row).not.toContainText("Figures match");
  await expect(row).toContainText("Totals only — tickets not checked");
  await row.getByRole("button", { name: "Details" }).click();
  await expect(page.getByRole("note").filter({ hasText: "Totals-only comparison — individual tickets not checked." }))
    .toBeVisible();
  await expect(page.getByRole("table", { name: "Ticket-by-ticket check" })).toHaveCount(0);
  // Viewers cannot enter or correct invoice lines.
  await expect(page.getByLabel("Ticket numbers listed on this invoice (optional)")).toHaveCount(0);
  await expect(row.getByRole("button", { name: "Correct" })).toHaveCount(0);
});

test("correcting an itemized line puts its ticket list back in the paste box", async ({ page }) => {
  await mockInvoices(page, "owner", [ITEMIZED]);
  await page.goto("/invoices");
  await page.getByRole("row").filter({ hasText: "EA-2026-1107" }).getByRole("button", { name: "Correct" }).click();
  await expect(page.getByLabel("Ticket numbers listed on this invoice (optional)"))
    .toHaveValue("010482\t24.500\n10490\t22.100");
});
