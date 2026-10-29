import { expect, test } from "@playwright/test";

const cors = {
  "access-control-allow-origin": "http://127.0.0.1:3000",
  "access-control-allow-credentials": "true",
  "access-control-allow-headers": "content-type, x-ticketflow-request",
  "access-control-allow-methods": "GET, POST, OPTIONS",
};
const json = (route, result, status = 200) => route.fulfill({
  status, headers: cors, contentType: "application/json", body: JSON.stringify(result),
});

test("signed-out visitor cannot reach ticket review workspace", async ({ page }) => {
  await page.route("**/api/auth/me", (route) => json(route, { detail: "Sign in" }, 401));
  await page.goto("/upload");
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Sign in to TicketFlow" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Original scanned ticket" })).toHaveCount(0);
});

test("sign-in renders a workspace and sign-out returns to login", async ({ page }) => {
  const calls = { login: 0, logout: 0 };
  await page.route("**/api/auth/**", (route) => {
    const { pathname } = new URL(route.request().url());
    if (route.request().method() === "OPTIONS") {
      return route.fulfill({ status: 204, headers: cors });
    }
    if (pathname === "/api/auth/login") {
      calls.login++;
      if (route.request().headers()["x-ticketflow-request"] !== "1") {
        return json(route, { detail: "Missing CSRF header" }, 403);
      }
      return json(route, { id: 1, username: "owner" });
    }
    if (pathname === "/api/auth/logout") {
      calls.logout++;
      return json(route, { ok: true });
    }
    if (pathname === "/api/auth/me") {
      return json(route, { id: 1, username: "owner", organization_name: "Original workspace" });
    }
    return json(route, { detail: "Not found" }, 404);
  });
  await page.route("**/api/dashboard*", (route) => json(route, {
    tickets: 0, needs_review: 0, flagged: 0, verified: 0,
    total_tons: 0, pending_tons: 0, total_cost: 0,
    unpriced_tickets: 0, pending_cost_tickets: 0,
    by_supplier: [], by_material: [], recent_batches: [],
  }));
  await page.route("**/api/projects*", (route) => json(route, []));

  await page.goto("/login");
  await page.getByLabel("Username").fill("owner");
  await page.getByLabel("Password").fill("correct-long-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL("/");
  await expect(page.getByRole("navigation", { name: "Main navigation" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  await expect(page.getByText("Original workspace")).toBeVisible();
  expect(calls.login).toBe(1);

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);
  expect(calls.logout).toBe(1);
});
