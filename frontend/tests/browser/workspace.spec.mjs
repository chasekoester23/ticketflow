import { expect, test } from "@playwright/test";

const cors = {
  "access-control-allow-origin": "http://127.0.0.1:3000",
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
    if (url.pathname === "/api/auth/me") return json(route, {
      id: 1, username: "owner", organization_name: "Original workspace",
    });
    if (url.pathname === "/api/projects") return json(route, []);
    if (url.pathname === "/api/records") return json(route, []);
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
  await expect(page.getByRole("heading", { name: "Delivery operations" })).toBeVisible();
  await expect(page.getByText("Original workspace")).toBeVisible();
  await expect(page.getByText("2,160.50", { exact: false })).toBeVisible();
  await expect(page.getByRole("link", { name: "New ticket upload" })).toBeVisible();
  await page.getByRole("link", { name: "Projects", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Project directory" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Projects", exact: true })).toHaveAttribute("aria-current", "page");
});

test("mobile navigation opens, navigates, and preserves the signed-in workspace", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockWorkspace(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Open navigation" }).click();
  await expect(page.getByRole("link", { name: "Ticket intake", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Close navigation" }).click();
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("link", { name: "Delivery records", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Delivery records", exact: true })).toBeVisible();
});

test("review-desk status shortcuts retain filter in the records workspace", async ({ page }) => {
  const calls = [];
  await mockWorkspace(page, calls);
  await page.goto("/");
  await page.getByRole("link", { name: /Awaiting verification/ }).click();
  await expect(page).toHaveURL(/\/records\?status=needs_review/);
  await expect(page.getByLabel("Review status")).toHaveValue("needs_review");
  await expect.poll(() => calls.filter((x) => x === "/api/records?status=needs_review").length).toBeGreaterThan(0);
});
