import { expect, test } from "@playwright/test";
import { chooseProject, json, mockWorkspace } from "./f_helpers.mjs";

const nav = (page) => page.getByRole("navigation", { name: "Main navigation" });

test("the chosen project follows a person across pages and a reload, and every request carries it", async ({ page }) => {
  const log = await mockWorkspace(page);
  await page.goto("/");
  await expect(page.getByTestId("overview-scope")).toHaveText("All projects");
  await chooseProject(page, "24-115");
  await expect(page.getByTestId("overview-scope")).toHaveText("24-115 · Fictional Road Job");
  await expect.poll(() => log.some((line) => line.startsWith("GET /api/dashboard?project_id=1"))).toBe(true);

  await nav(page).getByRole("link", { name: "Delivery Records", exact: true }).click();
  await expect(page).toHaveURL(/\/records\?project_id=1$/);
  await nav(page).getByRole("link", { name: /^Review Queue/ }).click();
  await expect.poll(() => log.some((line) => line === "GET /api/review-queue?project_id=1")).toBe(true);

  await page.reload();
  await expect(page.getByTestId("project-picker")).toContainText("24-115 · Fictional Road Job");
  await chooseProject(page, "All projects");
  await expect(page.getByTestId("project-picker")).toContainText("All projects");
});

test("a link that names a project chooses it in the top bar", async ({ page }) => {
  await mockWorkspace(page);
  await page.goto("/records?project_id=1&status=needs_review");
  await expect(page.getByTestId("project-picker")).toContainText("24-115");
  await expect(page.getByLabel("Review status")).toHaveValue("needs_review");
});

test("the review queue lists what's waiting and filters by kind", async ({ page }) => {
  await mockWorkspace(page);
  await page.goto("/review");
  await expect(page.getByRole("heading", { name: "Review Queue", exact: true })).toBeVisible();
  const row = page.getByRole("table", { name: "Review queue" }).getByRole("row").nth(1);
  await expect(row).toContainText("Delivery tickets · batch #5");
  await expect(row.getByRole("link", { name: "Review →" })).toHaveAttribute("href", "/upload?batch=5");
  await page.getByRole("button", { name: /Truck shifts/ }).click();
  await expect(page).toHaveURL(/\/review\?kind=truck_shifts$/);
  await expect(page.getByTestId("queue-empty")).toContainText("No truck shifts waiting");
});

test("the invoice register shows billed against approved records, and old line links still open the line", async ({ page }) => {
  await mockWorkspace(page);
  await page.goto("/invoices");
  const row = page.getByRole("table", { name: "Invoice register" }).getByRole("row").nth(1);
  await expect(row).toContainText("INV-4401");
  await expect(row).toContainText("$615.00");
  await expect(row).toContainText("Differences");
  await page.goto("/invoices?line=9");
  await expect(page).toHaveURL(/\/invoices\/lines\?line=9$/);
});

test("reports show approved costs from the server and download as Excel for the same dates", async ({ page }) => {
  const log = await mockWorkspace(page);
  await page.goto("/reports");
  await expect(page.getByRole("region", { name: "Verified costs" })).toContainText("$1,845.00");
  await page.getByLabel("From").fill("2026-10-01");
  await expect.poll(() => log.some((line) => line.includes("/api/reports/summary?date_from=2026-10-01&period=week"))).toBe(true);
  await expect(page.getByRole("link", { name: "Download Excel" })).toHaveAttribute("href", /summary\.xlsx\?date_from=2026-10-01&period=week/);
  await page.getByRole("tab", { name: "Material" }).click();
  await expect(page.getByRole("table", { name: "Quantity by supplier" })).toContainText("Example Aggregates");
});

test("the project directory shows open work and costs, and marks a missing rate as incomplete", async ({ page }) => {
  await mockWorkspace(page, { extra: (_request, path) => path === "/api/projects/directory" ? (route) => json(route, [{
    id: 1, code: "24-115", name: "Fictional Road Job", job_location: "Fictional City", description: null, status: "active", created_at: "2026-10-01T00:00:00Z",
    tickets: 10, open_tickets: 2, open_shifts: 1, verified_tickets: 7, verified_tons: "150.000", material_cost: "2700.00", unpriced_tickets: 1,
    trucking_cost: "990.00", verified_cost: "3690.00", last_activity: null }]) : null });
  await page.goto("/projects");
  const row = page.getByRole("table", { name: "Projects" }).getByRole("row").nth(1);
  await expect(row).toContainText("$3,690.00");
  await expect(row).toContainText("+ 1 without a rate");
  await expect(row).toContainText("incomplete");
  await expect(row.getByRole("link", { name: "3" })).toHaveAttribute("href", "/records?project_id=1&status=needs_review");
});
