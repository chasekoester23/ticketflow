import { expect, test } from "@playwright/test";
import { profile } from "./accounts.mjs";

const cors = {
  "access-control-allow-origin": `http://127.0.0.1:${process.env.TF_TEST_PORT ?? "3000"}`,
  "access-control-allow-credentials": "true",
  "access-control-allow-headers": "content-type, x-ticketflow-request",
  "access-control-allow-methods": "GET, POST, OPTIONS",
};
const json = (route, data, status = 200) => route.fulfill({
  contentType: "application/json", headers: cors, status, body: JSON.stringify(data),
});

async function mockWorkspace(page, calls = []) {
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    calls.push(url.pathname + url.search);
    if (route.request().method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (url.pathname === "/api/auth/me") return json(route, profile("owner"));
    if (url.pathname === "/api/projects") return json(route, []);
    if (url.pathname === "/api/review-queue") return json(route, { summary: { tickets: 3, failed_files: 0, truck_shifts: 0, invoice_lines: 0, total: 3 }, items: [] });
    if (url.pathname === "/api/reports/summary") return json(route, { period: "day", date_from: null, date_to: null,
      material: { tickets: 3, tons: "72.500", cost: "2160.50", unpriced: 0, waiting: 4, undated: { tickets: 0, tons: "0.000", cost: "0.00", unpriced: 0 }, by_material: [], by_supplier: [] },
      trucking: { shifts: 0, hours: "0.00", cost: "0.00", loads: 0, by_hauler: [], billing_methods: [] },
      costs: { material: "2160.50", trucking: "0.00", total: "2160.50", complete: true, unpriced: 0 },
      by_period: [], by_project: [], unit_prices: [], invoices: { invoices: 0, billed: "0", matched: 0, with_differences: 0, incomplete: 0, difference: "0", totals_only: 0, rows: [] } });
    if (url.pathname === "/api/records/search") return json(route, { rows: [], total: 0, page: 1, page_size: 50, summary: { records: 0, by_status: {}, verified_tons: "0", verified_cost: "0", verified_unpriced: 0 } });
    if (url.pathname === "/api/dashboard") return json(route, {
      tickets: 7, needs_review: 3, flagged: 1, verified: 3,
      total_tons: "72.500", pending_tons: "25.000", total_cost: "2160.50",
      unpriced_tickets: 0, pending_cost_tickets: 4, by_supplier: [], by_material: [],
      recent_batches: [{ id: 11, project: "North Bridge", files: 1,
        status: "ready_for_review", created_at: "2026-10-28T14:00:00Z" }],
    });
    return json(route, { detail: "Not found" }, 404);
  });
}

test("contractor navigation is identifiable and uses real operational data", async ({ page }) => {
  await mockWorkspace(page);
  await page.goto("/");
  await expect(page.getByRole("navigation", { name: "Main navigation" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Overview", exact: true })).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("heading", { name: "Overview", exact: true })).toBeVisible();
  await expect(page.getByText("Original workspace")).toBeVisible();
  await expect(page.getByRole("region", { name: "Approved costs" })).toContainText("$2,160.50");
  await expect(page.getByRole("button", { name: "Upload" })).toBeVisible();
  await page.getByRole("link", { name: "Projects", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Project directory" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Main navigation" }).getByRole("link", { name: "Projects", exact: true })).toHaveAttribute("aria-current", "page");
});

test("mobile navigation opens, navigates, and preserves the signed-in workspace", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockWorkspace(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Open navigation" }).click();
  // The pending count is part of the link's name, e.g. "Review Queue 3 to review".
  await expect(page.getByRole("link", { name: /^Review Queue \d+ to review$/ })).toBeVisible();
  // Tap the dimmed page beside the drawer, as a person does. The scrim's centre is under
  // the open drawer at this width, so a plain click raced the drawer's slide-in and hung
  // whenever the drawer arrived first (seen with the browser's CPU slowed).
  await page.getByRole("button", { name: "Close navigation" }).click({ position: { x: 370, y: 420 } });
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("link", { name: /^Review Queue \d+ to review$/ }).click();
  await expect(page.getByRole("heading", { name: "Review Queue", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("link", { name: "Delivery Records", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Delivery records", exact: true })).toBeVisible();
});

test("review-desk status shortcuts retain filter in the records workspace", async ({ page }) => {
  const calls = [];
  await mockWorkspace(page, calls);
  await page.goto("/review");
  await page.getByRole("link", { name: "Waiting tickets as a list" }).click();
  await expect(page).toHaveURL(/\/records\?status=needs_review/);
  await expect(page.getByLabel("Review status")).toHaveValue("needs_review");
  await expect.poll(() => calls.filter((x) => x.startsWith("/api/records/search?status=needs_review")).length).toBeGreaterThan(0);
});
