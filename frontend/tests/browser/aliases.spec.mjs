import { expect, test } from "@playwright/test";
import { profile } from "./accounts.mjs";

const cors = {
  "access-control-allow-origin": "http://127.0.0.1:3000",
  "access-control-allow-credentials": "true",
  "access-control-allow-headers": "content-type, x-ticketflow-request",
  "access-control-allow-methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
};
const json = (route, value, status = 200) => route.fulfill({
  status, contentType: "application/json", headers: cors, body: JSON.stringify(value),
});
const TICKET = {
  id: 11, batch_id: 7, file_id: 3, source_page: 1, position: 0, ticket_number: "88120", delivery_date: "2026-10-15",
  material_type: "No. 53 Stone", tonnage: "24.500", supplier: "Ex. Agg. North Yard", job_location: "North Lot", flags: [],
  status: "needs_review", tonnage_confirmed: false, error: null, unit_price_override: null, price_snapshot_state: "pending",
  unit_price: null, price_source: null, cost: null, duplicates: [], pricing_notes: ["Supplier not in list"],
  verified_by: null, verified_at: null, suggestions: { supplier: "Example Aggregates" },
  as_read: { ticket_number: "88120", supplier: "Ex. Agg. North Yard", tonnage: "24.8" },
};

test("review offers the catalog name; nothing changes until a person saves it", async ({ page }) => {
  const patches = [];
  await page.route("**/api/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return json(route, profile("clerk"));
    if (path === "/api/status") return json(route, { extraction_configured: true });
    if (path === "/api/projects") return json(route, []);
    if (path === "/api/suppliers") return json(route, [{ id: 1, name: "Example Aggregates", contact: null }]);
    if (path === "/api/materials") return json(route, [{ id: 2, name: "No. 53 Stone", unit: "ton" }]);
    if (path === "/api/batches/7") return json(route, { id: 7, project_id: 1, layout: "grid_2x2", intake_mode: "extract",
      status: "ready_for_review", created_at: "2026-11-05T10:00:00Z", files: [] });
    if (path === "/api/batches/7/tickets") return json(route, [TICKET]);
    if (path === "/api/tickets/11/audit") return json(route, []);
    if (path === "/api/tickets/11" && request.method() === "PATCH") {
      patches.push(request.postDataJSON());
      return json(route, { ...TICKET, supplier: "Example Aggregates", suggestions: {} });
    }
    return json(route, { detail: "Not found" }, 404);
  });
  await page.goto("/upload?batch=7");
  const supplier = page.getByLabel("Supplier", { exact: true });
  await expect(supplier).toHaveValue("Ex. Agg. North Yard");
  await expect(page.getByRole("button", { name: "Use your catalog name: Example Aggregates" })).toBeVisible();
  expect(patches).toEqual([]);
  // What was read stays visible when a person's value differs.
  await expect(page.getByText("Read from the ticket: 24.8")).toBeVisible();
  await expect(page.getByLabel(/^Tonnage \(tons\)/)).toHaveValue("24.500");

  await page.getByRole("button", { name: "Use your catalog name: Example Aggregates" }).click();
  await expect(supplier).toHaveValue("Example Aggregates");
  expect(patches).toEqual([]); // filled in, but not saved
  await expect(page.getByText("Read from the ticket: Ex. Agg. North Yard")).toBeVisible();
});

test("a manager saves another spelling; a viewer only reads", async ({ page }) => {
  const posted = [];
  let role = "manager";
  const rows = [];
  await page.route("**/api/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return json(route, profile(role));
    if (path === "/api/suppliers") return json(route, [{ id: 1, name: "Example Aggregates", contact: null }]);
    if (path === "/api/materials") return json(route, [{ id: 2, name: "No. 53 Stone", unit: "ton" }]);
    if (path === "/api/rates" || path === "/api/pricing/history") return json(route, []);
    if (path === "/api/name-aliases" && request.method() === "POST") {
      posted.push(request.postDataJSON());
      rows.push({ id: 1, kind: "supplier", alias: "Ex. Agg.", target: "Example Aggregates", added_by: "Mia Manager",
        added_at: "2026-11-05T10:00:00Z", removed_by: null, removed_at: null });
      return json(route, rows[0], 201);
    }
    if (path === "/api/name-aliases") return json(route, rows);
    return json(route, { detail: "Not found" }, 404);
  });
  await page.goto("/pricing");
  await page.getByLabel("Spelling on tickets").fill("Ex. Agg.");
  await page.getByLabel("Means").selectOption({ label: "Example Aggregates" });
  await page.getByRole("button", { name: "Save spelling" }).click();
  await expect.poll(() => posted).toEqual([{ kind: "supplier", alias: "Ex. Agg.", target_id: 1 }]);
  const table = page.getByRole("table", { name: "Other spellings" });
  await expect(table).toContainText("Ex. Agg.");
  await expect(table).toContainText("Mia Manager");

  role = "viewer";
  await page.reload();
  await expect(page.getByRole("table", { name: "Other spellings" })).toContainText("Ex. Agg.");
  await expect(page.getByRole("button", { name: "Save spelling" })).toHaveCount(0);
  await expect(page.getByRole("table", { name: "Other spellings" }).getByRole("button", { name: "Remove" })).toHaveCount(0);
});
