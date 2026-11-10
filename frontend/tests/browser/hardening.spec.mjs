import { expect, test } from "@playwright/test";
import { profile } from "./accounts.mjs";

const cors = {
  "access-control-allow-origin": `http://127.0.0.1:${process.env.TF_TEST_PORT ?? "3000"}`,
  "access-control-allow-credentials": "true",
  "access-control-allow-headers": "content-type, x-ticketflow-request",
  "access-control-allow-methods": "GET, POST, PUT, DELETE, OPTIONS",
};
const json = (route, value, status = 200) => route.fulfill({
  status, contentType: "application/json", headers: cors, body: JSON.stringify(value),
});

test("every page forbids framing and sends conservative security headers", async ({ request }) => {
  // Fetch the HTML directly: these are the headers the server sends, before
  // any client-side sign-in redirect runs.
  for (const path of ["/login", "/upload", "/team", "/account"]) {
    const response = await request.get(path);
    expect(response.status(), path).toBe(200);
    const headers = response.headers();
    expect(headers["x-frame-options"], path).toBe("DENY");
    expect(headers["content-security-policy"], path).toContain("frame-ancestors 'none'");
    expect(headers["content-security-policy"], path).toContain("object-src 'none'");
    expect(headers["x-content-type-options"], path).toBe("nosniff");
    expect(headers["referrer-policy"], path).toBe("no-referrer");
    expect(headers["x-powered-by"], path).toBeUndefined();
  }
});

async function mockPricing(page, role) {
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return json(route, profile(role));
    if (path === "/api/suppliers") return json(route, [{ id: 1, name: "Example Aggregates", contact: null }]);
    if (path === "/api/materials") return json(route, [{ id: 2, name: "No. 53 Stone", unit: "ton" }]);
    if (path === "/api/rates") return json(route, [{ id: 3, supplier_id: 1, material_id: 2, unit_price: "31.2500" }]);
    if (path === "/api/pricing/history") return json(route, [
      { id: 2, action: "rate_changed", supplier: "Example Aggregates", material: "No. 53 Stone",
        old: "30.0000", new: "31.2500", at: "2026-11-03T15:04:00Z", actor: "Mia Manager" },
      { id: 1, action: "rate_set", supplier: "Example Aggregates", material: "No. 53 Stone",
        old: null, new: "30.0000", at: "2026-11-01T15:04:00Z", actor: "Pat Boss" },
    ]);
    return json(route, { detail: "Not found" }, 404);
  });
}

test("rate history shows who changed a price, from old to new", async ({ page }) => {
  await mockPricing(page, "manager");
  await page.goto("/pricing");
  const history = page.locator("section", { has: page.getByRole("heading", { name: "Price change history" }) });
  const changed = history.getByRole("row", { name: /Rate changed/ });
  await expect(changed).toContainText("Example Aggregates · No. 53 Stone");
  await expect(changed).toContainText("$30.00");
  await expect(changed).toContainText("$31.25");
  await expect(changed).toContainText("Mia Manager");
  await expect(history.getByRole("row", { name: /Rate set/ })).toContainText("Pat Boss");
});

test("viewers can read rate history but not edit the catalog", async ({ page }) => {
  await mockPricing(page, "viewer");
  await page.goto("/pricing");
  await expect(page.getByRole("heading", { name: "Price change history" })).toBeVisible();
  await expect(page.getByText("Mia Manager")).toBeVisible();
  await expect(page.getByRole("button", { name: "Save unit price" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Remove" })).toHaveCount(0);
});
