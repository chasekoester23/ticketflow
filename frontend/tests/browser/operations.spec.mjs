import { expect, test } from "@playwright/test";
import { profile } from "./accounts.mjs";

const cors = {
  "access-control-allow-origin": `http://127.0.0.1:${process.env.TF_TEST_PORT ?? "3000"}`, "access-control-allow-credentials": "true",
  "access-control-allow-methods": "GET, POST, PUT, OPTIONS", "access-control-allow-headers": "content-type, x-ticketflow-request",
};
const json = (route, value, status = 200) => route.fulfill({ status, contentType: "application/json", headers: cors, body: JSON.stringify(value) });
const DASHBOARD = { tickets: 0, needs_review: 0, flagged: 0, verified: 0, total_tons: "0", pending_tons: "0", total_cost: "0", unpriced_tickets: 0, pending_cost_tickets: 0, by_supplier: [], by_material: [], recent_batches: [] };

async function mock(page, role, routes) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return json(route, profile(role));
    if (path === "/api/projects") return json(route, []);
    if (path === "/api/dashboard") return json(route, DASHBOARD);
    if (routes[path]) return routes[path](route, request);
    return json(route, { detail: "Unmocked " + path }, 404);
  });
}

const STATUS = {
  healthy: false, invoice_reader: "text_layer",
  checks: [
    { name: "Processing worker", state: "problem", message: "Not running, and 2 upload(s) are waiting on it.", action: "From the backend folder, run: python scripts/run_worker.py" },
    { name: "Backups", state: "ok", message: "Latest backup is 1 day(s) old.", action: null },
  ],
  queue: { pending: 2, running: 0, failed_jobs: 0, oldest_pending_seconds: 600 },
  worker: { running: false, last_seen_seconds: null },
  database: { kind: "sqlite", revision: "20261105_17", expected_revision: "20261105_17" },
};

test("system health says what's wrong and how to fix it", async ({ page }) => {
  await mock(page, "manager", { "/api/system/status": (route) => json(route, STATUS), "/api/onboarding": (route) => json(route, { complete: true, steps: [] }) });
  await page.goto("/system");
  const worker = page.getByRole("row").filter({ hasText: "Processing worker" });
  await expect(worker).toContainText("Problem");
  await expect(worker).toContainText("python scripts/run_worker.py");
  await expect(page.getByRole("row").filter({ hasText: "Backups" })).toContainText("OK");
  await expect(page.getByRole("region", { name: "Processing queue" })).toContainText("10 min");
  await expect(page.getByRole("link", { name: "System health" })).toBeVisible();
});

test("clerks don't see the system page in the menu", async ({ page }) => {
  await mock(page, "clerk", { "/api/onboarding": (route) => json(route, { complete: false, steps: [] }) });
  await page.goto("/");
  await expect(page.getByRole("navigation", { name: "Main navigation" })).toBeVisible();
  await expect(page.getByRole("link", { name: "System health" })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Getting started" })).toHaveCount(0);
});

test("getting started shows what's left and goes away when finished", async ({ page }) => {
  const state = { complete: false, steps: [
    { key: "project", title: "Create your first project", detail: "d", href: "/projects", done: true },
    { key: "catalog", title: "Add suppliers, materials and rates", detail: "d", href: "/pricing", done: false },
  ] };
  await mock(page, "owner", { "/api/onboarding": (route) => json(route, state) });
  await page.goto("/");
  const panel = page.getByRole("region", { name: "Getting started" });
  await expect(panel).toContainText("1 of 2 done");
  await expect(panel.getByRole("link", { name: "Add suppliers, materials and rates" })).toHaveAttribute("href", "/pricing");
  await expect(panel.getByRole("link", { name: "Create your first project" })).toHaveCount(0);
  state.complete = true;
  await page.reload();
  await expect(page.getByRole("heading", { name: "Delivery operations" })).toBeVisible();
  await expect(panel).toHaveCount(0);
});

test("an upload stuck behind a stopped worker says so plainly", async ({ page }) => {
  await mock(page, "owner", {
    "/api/status": (route) => json(route, { extraction_configured: false, model: "none", worker_running: false }),
    "/api/suppliers": (route) => json(route, []), "/api/materials": (route) => json(route, []),
    "/api/batches/5": (route) => json(route, { id: 5, project_id: null, layout: "single", intake_mode: "manual", status: "uploaded", created_at: "2026-11-05T00:00:00Z", files: [] }),
    "/api/batches/5/tickets": (route) => json(route, []),
    "/api/batches/5/manifest": (route) => json(route, { detail: "n/a" }, 404),
  });
  await page.goto("/upload?batch=5");
  await expect(page.getByTestId("queue-status")).toContainText("The processing worker isn't running, so this upload is waiting.");
});

test("a rate sheet previews first and only saves when told to", async ({ page }) => {
  const posts = [];
  const plan = { applied: false, adds: 1, changes: 1, unchanged: 1, new_suppliers: ["Sample Stone Co"], new_materials: [], plan: [
    { line: 1, supplier: "Example Aggregates", material: "Sand", price: "12.0000", old_price: "10.0000", action: "change", new_supplier: false, new_material: false },
    { line: 2, supplier: "Sample Stone Co", material: "Sand", price: "11.0000", old_price: null, action: "add", new_supplier: true, new_material: false },
    { line: 3, supplier: "Example Aggregates", material: "Gravel", price: "9.0000", old_price: "9.0000", action: "unchanged", new_supplier: false, new_material: false },
  ] };
  await mock(page, "manager", {
    "/api/suppliers": (route) => json(route, []), "/api/materials": (route) => json(route, []), "/api/rates": (route) => json(route, []),
    "/api/pricing/history": (route) => json(route, []), "/api/name-aliases": (route) => json(route, []),
    "/api/rates/import": (route, request) => { posts.push(request.postDataJSON()); return json(route, { ...plan, applied: request.postDataJSON().apply }); },
  });
  await page.goto("/pricing");
  await page.getByRole("textbox", { name: "Rate sheet" }).fill("Example Aggregates\tSand\t12");
  await page.getByRole("button", { name: "Check this sheet" }).click();
  await expect(page.getByTestId("sheet-summary")).toContainText("1 new · 1 changed · 1 unchanged");
  await expect(page.getByTestId("sheet-summary")).toContainText("adds supplier: Sample Stone Co");
  await expect(page.getByRole("table", { name: "What this sheet would change" }).getByRole("row").filter({ hasText: "$10.00" })).toContainText("Changed");
  expect(posts.map((p) => p.apply)).toEqual([false]);
  await page.getByRole("button", { name: "Save 2 rates" }).click();
  await expect(page.getByRole("status").filter({ hasText: "keep their locked prices" })).toBeVisible();
  expect(posts.map((p) => p.apply)).toEqual([false, true]);
});

test("a server error shows its reference to quote", async ({ page }) => {
  await mock(page, "manager", {
    "/api/system/status": (route) => json(route, { detail: "Something went wrong on our side, and nothing further was changed. If it keeps happening, quote reference abc123.", request_id: "abc123" }, 500),
  });
  await page.goto("/system");
  await expect(page.getByRole("alert").filter({ hasText: "quote reference abc123" })).toBeVisible();
});
