import { expect, test } from "@playwright/test";
import { cors, json, mockWorkspace } from "./f_helpers.mjs";

const row = (i) => ({ ticket_id: 5000 + i, batch_id: 7, ticket_number: String(20000 + i), delivery_date: "2026-09-14", supplier: "Example Aggregates", material: "No. 57 Stone", tons: "18.250", cost: "547.50" });
const unbilled = (overrides = {}) => ({
  project: { id: 1, code: "24-115", name: "Fictional Road Job" },
  total_tickets: 3, total_tons: "54.750", total_cost: "1642.50", truncated: false,
  rows: [row(1), row(2), row(3)],
  gaps: [{ supplier: "Example Aggregates", material: "No. 57 Stone", date_from: "2026-09-08", date_to: "2026-09-19", tickets: 3, tons: "54.750" }],
  ...overrides,
});
const answering = (path, answer) => (request, actual) => actual === path ? answer : null;
const csv = (route, text) => route.fulfill({ status: 200, headers: { ...cors, "content-type": "text/csv; charset=utf-8" }, body: text });

test("unbilled deliveries show totals, gaps and each ticket, and say this isn't payment approval", async ({ page }) => {
  await mockWorkspace(page, { extra: answering("/api/reports/project/1/unbilled", (route) => json(route, unbilled())) });
  await page.goto("/reports/project?project_id=1");
  const section = page.getByRole("region", { name: "Unbilled deliveries" });
  await expect(section).toContainText("For investigation, not approval for payment.");
  const totals = section.getByTestId("unbilled-totals");
  await expect(totals).toContainText("3");
  await expect(totals).toContainText("54.75");
  await expect(totals).toContainText("$1,642.50");
  const gap = section.getByRole("table", { name: "Delivery dates no invoice line covers" }).getByRole("row").nth(1);
  await expect(gap).toContainText("2026-09-08 to 2026-09-19");
  await expect(gap.getByRole("link")).toHaveAttribute("href", /\/records\?project_id=1&status=verified&supplier=Example\+Aggregates&material=No\.\+57\+Stone&date_from=2026-09-08&date_to=2026-09-19/);
  const ticket = section.getByRole("table", { name: "Unbilled approved tickets" }).getByRole("row").filter({ hasText: "20001" });
  await expect(ticket).toContainText("18.25");
  await expect(ticket.getByRole("link", { name: /^Scan/ })).toHaveAttribute("href", /\/api\/tickets\/5001\/image$/);
  await expect(ticket.getByRole("link", { name: /^Ticket/ })).toHaveAttribute("href", "/upload?batch=7&ticket=5001");
  await expect(section.getByTestId("unbilled-truncated")).toHaveCount(0);
});

test("past 500 rows it says the list is cut short and the totals and CSV aren't", async ({ page }) => {
  const rows = Array.from({ length: 500 }, (_, i) => row(i));
  await mockWorkspace(page, { extra: answering("/api/reports/project/1/unbilled", (route) => json(route, unbilled({ total_tickets: 812, rows, truncated: true }))) });
  await page.goto("/reports/project?project_id=1");
  const section = page.getByRole("region", { name: "Unbilled deliveries" });
  await expect(section.getByTestId("unbilled-truncated")).toHaveText("Showing the first 500 of 812 tickets. The totals above, and the CSV, cover all of them.");
  await expect(section.getByRole("table", { name: "Unbilled approved tickets" }).getByRole("row")).toHaveCount(501);
});

test("the unbilled CSV downloads every row", async ({ page }) => {
  const asked = [];
  await mockWorkspace(page, { extra: (request, path) => {
    if (path === "/api/reports/project/1/unbilled") return (route) => json(route, unbilled());
    if (path === "/api/reports/project/1/unbilled.csv") { asked.push(path); return (route) => csv(route, "ticket_number,tons\n20001,18.250\n"); }
    return null;
  } });
  await page.goto("/reports/project?project_id=1");
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download every ticket (CSV)" }).click();
  expect((await download).suggestedFilename()).toBe("ticketflow-unbilled-24-115.csv");
  expect(asked).toEqual(["/api/reports/project/1/unbilled.csv"]);
});

test("nothing unbilled says so plainly", async ({ page }) => {
  await mockWorkspace(page, { extra: answering("/api/reports/project/1/unbilled", (route) => json(route, unbilled({ total_tickets: 0, total_tons: "0", total_cost: "0", rows: [], gaps: [] }))) });
  await page.goto("/reports/project?project_id=1");
  await expect(page.getByRole("region", { name: "Unbilled deliveries" })).toContainText("Every approved delivery on this project is listed or covered by an invoice line.");
  await expect(page.getByRole("button", { name: "Download every ticket (CSV)" })).toHaveCount(0);
});

test("a server without the unbilled check yet leaves the rest of the report working", async ({ page }) => {
  await mockWorkspace(page);
  await page.route("**/api/reports/project/1/unbilled", (route) => json(route, { detail: "Not Found" }, 404));
  await page.goto("/reports/project?project_id=1");
  await expect(page.getByRole("region", { name: "Unbilled deliveries" })).toContainText("doesn't offer the unbilled check yet");
  await expect(page.getByRole("region", { name: "Project totals" })).toContainText("41.00");
  await expect(page.getByRole("alert").filter({ hasText: /\S/ })).toHaveCount(0);
});

test("report figures lead to the tickets and invoice lines behind them", async ({ page }) => {
  await mockWorkspace(page);
  await page.goto("/reports/project?project_id=1");
  const pair = page.getByRole("region", { name: "By supplier and material" }).getByRole("row").filter({ hasText: "Example Aggregates" });
  await expect(pair.getByRole("link", { name: /approved tickets/ })).toHaveAttribute("href", "/records?project_id=1&status=verified&supplier=Example+Aggregates&material=No.+57+Stone");
  const month = page.getByRole("region", { name: "By delivery month" }).getByRole("row").filter({ hasText: "2026-10" });
  await expect(month.getByRole("link", { name: /approved tickets/ })).toHaveAttribute("href", "/records?project_id=1&status=verified&date_from=2026-10-01&date_to=2026-10-31");
  const line = page.getByRole("region", { name: "Invoice lines" }).getByRole("link", { name: /EA-2026-1107/ });
  await expect(line).toHaveAttribute("href", "/invoices?line=9");
  await line.click();
  await expect(page.getByRole("region", { name: "Why this result" })).toBeVisible();
  await expect(page.getByRole("heading", { name: /Invoice EA-2026-1107/ })).toBeVisible();
});

test("the differences CSV goes out for the chosen project and supplier", async ({ page }) => {
  const asked = [];
  await mockWorkspace(page, { extra: (request, path, method, url) => {
    if (path !== "/api/invoice-comparisons/discrepancies.csv") return null;
    asked.push(url.search);
    return (route) => csv(route, "invoice,supplier,kind\nEA-2026-1107,Example Aggregates,not_found\n");
  } });
  await page.goto("/invoices");
  const group = page.getByRole("group", { name: "Differences to send to a supplier" });
  await expect(group).toContainText("not approval for payment");
  await group.getByLabel("Project").selectOption("1");
  await group.getByLabel("Supplier").selectOption("Example Aggregates");
  const download = page.waitForEvent("download");
  await group.getByRole("button", { name: "Download discrepancy list (CSV)" }).click();
  expect((await download).suggestedFilename()).toBe("ticketflow-invoice-differences-24-115-Example_Aggregates.csv");
  expect(asked).toEqual(["?project_id=1&supplier=Example+Aggregates"]);
});

test("a server without the differences CSV yet says so instead of breaking", async ({ page }) => {
  await mockWorkspace(page);
  await page.goto("/invoices");
  await page.getByRole("button", { name: "Download discrepancy list (CSV)" }).click();
  await expect(page.getByRole("group", { name: "Differences to send to a supplier" }).getByRole("alert")).toContainText("doesn't offer that download yet");
});

test("the overview's counts open the lines they count", async ({ page }) => {
  await mockWorkspace(page);
  await page.goto("/invoices");
  await page.getByRole("region", { name: "Invoice overview" }).getByRole("button", { name: "Show them" }).click();
  await expect(page.getByRole("button", { name: "Differences", exact: true })).toHaveAttribute("aria-pressed", "true");
});
