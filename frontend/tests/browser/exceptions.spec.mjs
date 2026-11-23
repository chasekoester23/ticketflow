import { expect, test } from "@playwright/test";
import { profile } from "./accounts.mjs";

const cors = {
  "access-control-allow-origin": `http://127.0.0.1:${process.env.TF_TEST_PORT ?? "3000"}`, "access-control-allow-credentials": "true",
  "access-control-allow-methods": "GET, POST, OPTIONS", "access-control-allow-headers": "content-type, x-ticketflow-request, if-match",
};
const json = (route, value, status = 200) => route.fulfill({ status, contentType: "application/json", headers: cors, body: JSON.stringify(value) });

const base = { project_id: 7, project: "P1", supplier: "Example Aggregates", material: "No. 53 Crushed Stone", ticket_ids: [],
  billed_tons: null, approved_tons: null, line_billed: "531.00", line_approved: "410.38", unbilled_tickets: null, detected: true,
  status: "open", assignee_user_id: null, assignee: null, recovered_amount: null, note: null, revision: "new", updated_at: null, updated_by: null };
const ITEMS = [
  { ...base, key: "D:ticket_on_two_invoices:1,2", category: "duplicate", category_label: "Possible duplicate charge", severity: "high",
    title: "Ticket 10482 is on 2 invoice lines", explanation: "Example Aggregates: INV-1, INV-2. It may be billed twice.",
    invoice_numbers: ["INV-1", "INV-2"], line_ids: [1, 2], ticket_number: null, amount: null, line_billed: null, line_approved: null },
  { ...base, key: "L1:not_found:10499", category: "missing_ticket", category_label: "Billed delivery with no matching record", severity: "medium",
    title: "No delivery record: invoice INV-1, ticket 10499", explanation: "Find the paper for 10499, or ask the supplier for a copy.",
    invoice_numbers: ["INV-1"], line_ids: [1], ticket_number: "10499", billed_tons: "5.000", amount: "90.00" },
];
const PAGE = (items, canManage = true) => ({
  summary: { open: items.length, by_category: { rate: 0, quantity: 0, duplicate: 1, missing_ticket: 1, not_invoiced: 0, unmatched_line: 0, needs_review: 0, invoice_arithmetic: 0 },
    by_severity: { high: 1, medium: 1, low: 0 }, potential_overcharge: "90.00", billed_below_approved: "0", disputed_amount: "0",
    recovered: "0", money_unknown: 0, unassigned: items.length },
  items, cleared: [], categories: { rate: "Rate differs from agreement", quantity: "Billed tons differ from deliveries", duplicate: "Possible duplicate charge",
    missing_ticket: "Billed delivery with no matching record", not_invoiced: "Approved delivery not billed", unmatched_line: "Invoice line with nothing to compare",
    needs_review: "Needs a person to decide", invoice_arithmetic: "Invoice arithmetic" },
  people: [{ id: 1, name: "Pat Manager" }], projects: [{ id: 7, code: "P1" }], can_manage: canManage,
  note: "Each amount is one cause of an invoice line's billed-minus-approved difference, counted once.",
});

async function mock(page, role, { onReview, items = ITEMS } = {}) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (url.pathname === "/api/auth/me") return json(route, profile(role));
    if (url.pathname === "/api/exceptions") return json(route, PAGE(items, role !== "viewer"));
    if (url.pathname === "/api/exceptions/history") return json(route, []);
    if (url.pathname === "/api/exceptions/review") return onReview(route, request);
    return json(route, { detail: "Unmocked " + url.pathname }, 404);
  });
}

test("the exceptions center shows totals, lists by severity, and saves an owner with the revision", async ({ page }) => {
  const posts = [];
  await mock(page, "manager", { onReview: (route, request) => {
    posts.push({ body: request.postDataJSON(), ifMatch: request.headers()["if-match"] });
    return json(route, { ...ITEMS[1], status: "investigating", assignee_user_id: 1, assignee: "Pat Manager", revision: "1" });
  } });
  await page.goto("/invoices/exceptions");
  const totals = page.getByRole("region", { name: "Exception totals" });
  await expect(totals).toContainText("Potential overcharge");
  await expect(totals).toContainText("$90.00");
  const rows = page.getByRole("table", { name: "Exception list" }).getByRole("row");
  await expect(rows.nth(1)).toContainText("high");
  await expect(rows.nth(2)).toContainText("+$90.00");

  await rows.nth(2).click();
  const detail = page.getByTestId("exception-detail");
  await expect(detail).toContainText("Find the paper for 10499");
  await expect(detail.getByRole("link", { name: /Invoice INV-1/ })).toHaveAttribute("href", "/invoices?line=1");
  await expect(detail.getByRole("link", { name: /Ticket 10499 in records/ })).toHaveAttribute("href", "/records?q=10499");
  await detail.getByLabel("Owner").selectOption("1");
  await detail.getByLabel("Status").selectOption("investigating");
  await detail.getByRole("button", { name: "Save" }).click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0]).toEqual({ body: { key: "L1:not_found:10499", status: "investigating", assignee_user_id: 1 }, ifMatch: "new" });
  await expect(page).toHaveURL(/key=L1%3Anot_found%3A10499/);
});

test("closing an exception asks why, and a viewer sees it read-only", async ({ page }) => {
  await mock(page, "manager", { onReview: (route) => json(route, { detail: "Say why, so the next person doesn't reopen the same question." }, 422) });
  await page.goto("/invoices/exceptions?key=L1%3Anot_found%3A10499");
  const detail = page.getByTestId("exception-detail");
  await detail.getByLabel("Status").selectOption("dismissed");
  await expect(detail.getByText("Why (required)")).toBeVisible();
  await detail.getByRole("button", { name: "Save" }).click();
  await expect(detail.getByRole("alert")).toContainText("Say why");

  await page.unrouteAll({ behavior: "ignoreErrors" });
  await mock(page, "viewer");
  await page.goto("/invoices/exceptions?key=L1%3Anot_found%3A10499");
  await expect(page.getByTestId("exception-detail")).toContainText("You can see where each one stands");
  await expect(page.getByTestId("exception-detail").getByRole("button", { name: "Save" })).toHaveCount(0);
});

test("with nothing to question, the page says so plainly", async ({ page }) => {
  await mock(page, "manager", { items: [] });
  await page.goto("/invoices/exceptions");
  await expect(page.getByTestId("exceptions-empty")).toContainText("Nothing to question");
});

test("a closed exception whose figures changed is listed again, with the person's decision shown as it was", async ({ page }) => {
  const changed = { ...ITEMS[1], status: "dismissed", changed_since_closed: true, needs_attention: true, updated_by: "Pat Manager", revision: "3" };
  await mock(page, "manager", { items: [changed] });
  await page.goto("/invoices/exceptions?key=L1%3Anot_found%3A10499");
  const row = page.getByRole("table", { name: "Exception list" }).getByRole("row").nth(1);
  await expect(row).toContainText("Not an issue");
  await expect(row).toContainText("Figures changed");
  await expect(page.getByTestId("changed-since-closed")).toContainText("Marked “Not an issue” by Pat Manager, but its figures have changed since");
});
