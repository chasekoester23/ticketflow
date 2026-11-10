import { expect, test } from "@playwright/test";
import { profile } from "./accounts.mjs";

// Fictional supplier, project, tickets and invoice. Mirrors the API contract
// in backend/app/routers/invoices.py.
const cors = {
  "access-control-allow-origin": `http://127.0.0.1:${process.env.TF_TEST_PORT ?? "3000"}`,
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

test("a manager confirms clean matches; people's confirmation is reported separately", async ({ page }) => {
  const calls = [];
  const row = { ...ITEMIZED.ticket_check[0], evidence: "candidate", link_id: null };
  const before = { ...ITEMIZED, ticket_check: [row], evidence: { listed_tickets: 2, confirmed: 0, candidates: 1, conflicting: 0, invalidated: 0, unmatched: 1, confirmed_tons: "0" } };
  const after = { ...before, ticket_check: [{ ...row, evidence: "confirmed", link_id: 4 }], evidence: { ...before.evidence, confirmed: 1, candidates: 0, confirmed_tons: "24.500" } };
  let current = before;
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return json(route, profile("manager"));
    if (path === "/api/projects") return json(route, []);
    if (path === "/api/suppliers" || path === "/api/materials") return json(route, []);
    if (path === "/api/invoice-comparisons") return json(route, [current]);
    if (request.method() === "POST") {
      calls.push({ path, body: request.postData() });
      current = path.endsWith("/release") ? before : after;
      return json(route, current);
    }
    return json(route, { detail: "Not found" }, 404);
  });
  await page.goto("/invoices");
  await page.getByRole("button", { name: "Details" }).click();
  const summary = page.getByTestId("evidence-summary");
  await expect(summary).toContainText("0 of 2 invoice tickets confirmed by a person");
  await page.getByRole("button", { name: "Confirm 1 clean match" }).click();
  await expect(summary).toContainText("1 of 2 invoice tickets confirmed by a person");
  expect(calls[0].path).toBe("/api/invoice-comparisons/9/ticket-links");
  expect(JSON.parse(calls[0].body)).toEqual({ ticket_ids: [11] });
  const table = page.getByRole("table", { name: "Ticket-by-ticket check" });
  await expect(table.getByRole("row").filter({ hasText: "010482" })).toContainText("Confirmed");
  await table.getByRole("button", { name: "Release" }).click();
  await expect(summary).toContainText("0 of 2 invoice tickets confirmed by a person");
  expect(calls[1].path).toBe("/api/invoice-comparisons/9/ticket-links/4/release");
});

test("correcting an itemized line puts its ticket list back in the paste box", async ({ page }) => {
  await mockInvoices(page, "owner", [ITEMIZED]);
  await page.goto("/invoices");
  await page.getByRole("row").filter({ hasText: "EA-2026-1107" }).getByRole("button", { name: "Correct" }).click();
  await expect(page.getByLabel("Ticket numbers listed on this invoice (optional)"))
    .toHaveValue("010482\t24.500\n10490\t22.100");
});

const DIAGNOSED = line({
  status: "discrepancy", comparison_mode: "itemized", quantity_difference: "0.000", subtotal_difference: "45.00",
  diagnosis: {
    headline: "The totals agree, but individual tickets don't.",
    tons: {
      start: "46.600", billed: "46.600", complete: true, rows_without_tons: 0, unexplained: "0.000",
      items: [
        { kind: "not_found", label: "Billed on tickets with no delivery record", tons: "22.100", count: 1, tickets: ["10490"] },
        { kind: "not_on_invoice", label: "Verified deliveries this invoice doesn't list", tons: "-22.100", count: 1, tickets: ["10483"] },
      ],
    },
    money: {
      start: "1353.00", quantity_effect: "0.00", rate_effect: "45.00", arithmetic_effect: "0.00", rounding: "0.00", billed: "1398.00",
      rate_groups: [{ locked_rate: "29.0000", tickets: 2, tons: "46.600", effect: "45.00" }],
    },
    money_unresolved_reason: null,
    next_steps: [
      { text: "Find the paper for 10490, or ask the supplier for a copy.", href: null },
      { text: "Review 2 delivery record(s) that could belong to this line.", href: "/records?project_id=1&status=needs_review" },
    ],
  },
});
const OVERVIEW = {
  lines: 2, by_status: { discrepancy: 1, incomplete: 1 }, billed: "2796.00", verified: "1353.00", lines_unresolved: 1,
  by_supplier: [
    { supplier: "Example Aggregates", lines: 1, billed: "1398.00", verified: "1353.00", lines_unresolved: 0, open: 1 },
    { supplier: "Sample Stone Co", lines: 1, billed: "1398.00", verified: "0", lines_unresolved: 1, open: 1 },
  ],
  exceptions: [{ kind: "ticket_on_two_invoices", title: "Ticket 10482 is on 2 invoice lines", detail: "Example Aggregates: EA-2026-1107, EA-2026-1200. It may be billed twice.", line_ids: [9] }],
};

async function mockDiagnosis(page, role = "viewer") {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return json(route, profile(role));
    if (path === "/api/projects") return json(route, [{ id: 1, code: "24-115", name: "Fictional Road Job", job_location: null, description: null, status: "active" }]);
    if (path === "/api/suppliers" || path === "/api/materials") return json(route, []);
    if (path === "/api/invoice-comparisons/overview") return json(route, OVERVIEW);
    if (path === "/api/invoice-comparisons") return json(route, [DIAGNOSED, line({ id: 10, invoice_number: "SS-7", supplier_name: "Sample Stone Co", status: "incomplete" })]);
    return json(route, { detail: "Not found" }, 404);
  });
}

test("details lead with why, in figures that land exactly on the bill", async ({ page }) => {
  await mockDiagnosis(page);
  await page.goto("/invoices");
  await page.getByRole("row").filter({ hasText: "EA-2026-1107" }).getByRole("button", { name: "Details" }).click();
  await expect(page.getByTestId("diagnosis-headline")).toHaveText("The totals agree, but individual tickets don't.");
  const bridge = page.getByRole("table", { name: "From approved tons to billed tons" });
  await expect(bridge.getByRole("row").filter({ hasText: "no delivery record" })).toContainText("10490");
  await expect(bridge.getByRole("row").filter({ hasText: "no delivery record" })).toContainText("+22.100");
  await expect(bridge.getByRole("row").filter({ hasText: "doesn't list" })).toContainText("-22.100");
  await expect(bridge.getByRole("row").filter({ hasText: "Billed tons" })).toContainText("46.6");
  await expect(bridge).not.toContainText("Not explained");
  const cost = page.getByRole("table", { name: "From approved cost to billed subtotal" });
  await expect(cost.getByRole("row").filter({ hasText: "locked $29.00" })).toContainText("+$45.00");
  await expect(page.getByText("Find the paper for 10490")).toBeVisible();
  await expect(page.getByRole("link", { name: /Review 2 delivery record/ })).toHaveAttribute("href", "/records?project_id=1&status=needs_review");
});

test("the overview totals what's known, says what's left out, and links billing patterns to their line", async ({ page }) => {
  await mockDiagnosis(page);
  await page.goto("/invoices");
  const overview = page.getByRole("region", { name: "Invoice overview" });
  await expect(overview).toContainText("$2,796.00");
  await expect(overview).toContainText("1 line(s) unresolved and left out");
  const patterns = page.getByRole("note", { name: "Billing patterns to check" });
  await expect(patterns).toContainText("Ticket 10482 is on 2 invoice lines");
  await expect(patterns).toContainText("It may be billed twice.");
  await patterns.getByRole("button", { name: "Open EA-2026-1107" }).click();
  await expect(page.getByTestId("diagnosis-headline")).toBeVisible();
});

test("the line list filters by result and searches, without losing the count", async ({ page }) => {
  await mockDiagnosis(page);
  await page.goto("/invoices");
  await expect(page.getByText("2 material lines")).toBeVisible();
  await page.getByRole("button", { name: "Differences" }).click();
  await expect(page.getByText("1 of 2 material lines")).toBeVisible();
  await expect(page.getByRole("row").filter({ hasText: "SS-7" })).toHaveCount(0);
  await page.getByRole("button", { name: "All", exact: true }).click();
  await page.getByRole("searchbox", { name: "Search invoice lines" }).fill("sample stone");
  await expect(page.getByRole("row").filter({ hasText: "EA-2026-1107" })).toHaveCount(0);
  await expect(page.getByRole("row").filter({ hasText: "SS-7" })).toBeVisible();
});

test("the entry form stays out of the way once lines exist, for managers only", async ({ page }) => {
  await mockDiagnosis(page, "manager");
  await page.goto("/invoices");
  await expect(page.getByRole("row").filter({ hasText: "EA-2026-1107" })).toBeVisible();
  await expect(page.getByLabel("Invoice number")).toHaveCount(0);
  await page.getByRole("button", { name: "Record an invoice line" }).click();
  await expect(page.getByLabel("Invoice number")).toBeVisible();
  await page.getByRole("button", { name: "Close" }).click();
  await expect(page.getByLabel("Invoice number")).toHaveCount(0);
});
