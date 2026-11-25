import { expect, test } from "@playwright/test";
import { json, mockWorkspace } from "./f_helpers.mjs";

const SUMMARY = {
  period: "week", date_from: null, date_to: null,
  material: { tickets: 412, tons: "8412.250", cost: "155626.63", unpriced: 6, waiting: 37, undated: { tickets: 0, tons: "0.000", cost: "0.00", unpriced: 0 },
    by_material: [{ name: "No. 57 Stone", tickets: 300, tons: "6200.500", cost: "114709.25", unpriced: 0 }, { name: "Rip Rap", tickets: 6, tons: "120.000", cost: "0.00", unpriced: 6 }],
    by_supplier: [{ name: "Example Aggregates", tickets: 412, tons: "8412.250", cost: "155626.63", unpriced: 6 }] },
  trucking: { shifts: 40, hours: "380.00", cost: "41800.00", loads: 260, by_hauler: [], billing_methods: [{ method: "hourly", cost: "41800.00" }] },
  costs: { material: "155626.63", trucking: "41800.00", total: "197426.63", complete: false, unpriced: 6 },
  by_period: [{ period: "2026-11-02", tickets: 200, tons: "4100.000", material: "75850.00", unpriced: 0, hours: "190.00", trucking: "20900.00", loads: 130, total: "96750.00" },
    { period: "2026-11-09", tickets: 212, tons: "4312.250", material: "79776.63", unpriced: 6, hours: "190.00", trucking: "20900.00", loads: 130, total: "100676.63" }],
  by_project: [{ id: 1, code: "24-115", name: "Fictional Road Job", status: "active", tickets: 300, tons: "6200.500", material: "114709.25", unpriced: 0, hours: "300.00", trucking: "33000.00", total: "147709.25" }],
  unit_prices: [],
  invoices: { invoices: 0, billed: "0", matched: 0, with_differences: 0, incomplete: 0, difference: "0", totals_only: 0, rows: [] },
};
const QUEUE = { summary: { tickets: 37, failed_files: 1, truck_shifts: 3, invoice_lines: 0, total: 41 }, items: [] };
const EXCEPTIONS = { summary: { open: 7, by_category: {}, by_severity: { high: 2, medium: 3, low: 2 }, potential_overcharge: "1240.50",
  billed_below_approved: "0", disputed_amount: "0", recovered: "0", money_unknown: 0, unassigned: 4 }, items: [] };

async function mock(page, overrides = {}) {
  const log = [];
  await mockWorkspace(page, { extra: (request, path) => {
    if (path === "/api/reports/summary") { log.push(new URL(request.url()).search); return (route) => json(route, overrides.summary ?? SUMMARY, overrides.status ?? 200); }
    return { "/api/review-queue": (route) => json(route, QUEUE), "/api/exceptions": (route) => json(route, EXCEPTIONS),
      "/api/onboarding": (route) => json(route, { complete: true, steps: [] }) }[path];
  } });
  return log;
}

test("the overview leads with approved material and hauling cost, and discloses missing rates", async ({ page }) => {
  await mock(page);
  await page.goto("/");
  const figures = page.getByRole("region", { name: "Approved costs" });
  await expect(figures).toContainText("8,412.3 t");
  await expect(figures).toContainText("$155,626.63");
  await expect(figures).toContainText("Incomplete: 6 approved without a rate");
  await expect(figures).toContainText("$41,800.00");
  await expect(figures).toContainText("$197,426.63");
  await expect(page.getByRole("figure", { name: "Spending over time" })).toBeVisible();
  await expect(page.getByRole("list", { name: "Material cost by material" })).toContainText("6 without a rate");
});

test("work waiting for people is one line that leads to where it's done", async ({ page }) => {
  await mock(page);
  await page.goto("/");
  const waiting = page.getByLabel("Waiting for people");
  await expect(waiting.getByRole("link", { name: "41 items in the Review Queue" })).toHaveAttribute("href", "/review");
  await expect(waiting.getByRole("link", { name: "7 open invoice exceptions" })).toHaveAttribute("href", "/invoices/exceptions");
  await expect(waiting.getByRole("link", { name: /6 approved deliveries without a rate/ })).toHaveAttribute("href", "/pricing");
  await expect(page.getByRole("region", { name: "Needs attention" })).toHaveCount(0);
});

test("each project row opens that project's workspace, and the date range is sent to the server", async ({ page }) => {
  const log = await mock(page);
  await page.goto("/");
  const row = page.getByRole("table", { name: "Project performance" }).getByRole("row").nth(1);
  await expect(row).toContainText("$147,709.25");
  await expect(row.getByRole("link", { name: "24-115" })).toHaveAttribute("href", "/projects/1");
  await page.getByRole("button", { name: "All dates" }).click();
  await expect.poll(() => log.at(-1)).toBe("?period=month");
});

test("if the figures can't be loaded, it says so instead of showing zero", async ({ page }) => {
  await mock(page, { summary: { detail: "Server unavailable" }, status: 503 });
  await page.goto("/");
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByRole("region", { name: "Approved costs" })).toHaveCount(0);
});
