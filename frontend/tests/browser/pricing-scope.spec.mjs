import { expect, test } from "@playwright/test";
import { profile } from "./accounts.mjs";

const cors = {
  "access-control-allow-origin": `http://127.0.0.1:${process.env.TF_TEST_PORT ?? "3000"}`, "access-control-allow-credentials": "true",
  "access-control-allow-methods": "GET, POST, PUT, DELETE, OPTIONS", "access-control-allow-headers": "content-type, x-ticketflow-request",
};
const json = (route, value, status = 200) => route.fulfill({ status, contentType: "application/json", headers: cors, body: JSON.stringify(value) });

const PROJECTS = [
  { id: 7, code: "P1", name: "North Garage", job_location: null, description: null, status: "active" },
  { id: 8, code: "P2", name: "Bridge Deck", job_location: null, description: null, status: "active" },
];
const RATES = [
  { id: 3, supplier_id: 1, material_id: 2, unit_price: "19.5000", project_id: null, effective_from: "2026-11-01" },
  { id: 4, supplier_id: 1, material_id: 2, unit_price: "16.7500", project_id: 7, effective_from: null },
  { id: 2, supplier_id: 1, material_id: 2, unit_price: "18.0000", project_id: null, effective_from: null },
];

const TIMELINE = { today: "2026-11-24", pairs: [{ supplier: "Example Aggregates", material: "No. 53 Crushed Stone", waiting_unpriced: 2, warnings: 2, scopes: [
  { project_id: null, project: null, label: "Company rate", segments: [
    { rate_id: 2, price: "18.0000", from: "2026-01-01", to: "2026-09-30", declared_to: "2026-09-30", state: "past" },
    { rate_id: 3, price: "19.5000", from: "2026-12-01", to: null, declared_to: null, state: "future" }],
    warnings: [{ kind: "gap", text: "Company rate: no price from 2026-10-01 to 2026-11-30. Tickets delivered then stay unpriced." }] },
  { project_id: 7, project: "P1", label: "P1 agreement", segments: [{ rate_id: 4, price: "16.7500", from: null, to: "2026-10-31", declared_to: "2026-10-31", state: "past" }],
    warnings: [{ kind: "expired", text: "P1 agreement: expired 2026-10-31. Tickets delivered after that stay unpriced until a new rate is set. The company rate is not used instead." }] },
] }] };

async function mock(page, posts) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return json(route, profile("manager"));
    if (path === "/api/projects") return json(route, PROJECTS);
    if (path === "/api/suppliers") return json(route, [{ id: 1, name: "Example Aggregates", contact: null }]);
    if (path === "/api/materials") return json(route, [{ id: 2, name: "No. 53 Crushed Stone", unit: "ton" }]);
    if (path === "/api/rates" && request.method() === "PUT") { posts.push(request.postDataJSON()); return json(route, { id: 9, ...request.postDataJSON() }); }
    if (path === "/api/rates") return json(route, RATES);
    if (path === "/api/rates/import") {
      posts.push(request.postDataJSON());
      return json(route, { applied: false, adds: 1, changes: 0, unchanged: 0, new_suppliers: [], new_materials: [], scope: "Project P2 (Bridge Deck), from 2026-12-01",
        plan: [{ line: 1, supplier: "Example Aggregates", material: "No. 53 Crushed Stone", price: "16.4000", old_price: null, action: "add", new_supplier: false, new_material: false }] });
    }
    if (path === "/api/pricing/history") return json(route, [{ id: 1, action: "rate_set", supplier: "Example Aggregates", material: "No. 53 Crushed Stone",
      old: null, new: "16.7500", scope: "Project P1 (North Garage)", at: "2026-11-20T15:00:00Z", actor: "Pat Manager" }]);
    if (path === "/api/name-aliases") return json(route, []);
    if (path === "/api/rates/timeline") return json(route, TIMELINE);
    return json(route, { detail: "Unmocked " + path }, 404);
  });
}

test("the rate schedule shows each rate's project and start date, in lookup order", async ({ page }) => {
  await mock(page, []);
  await page.goto("/pricing");
  const rows = page.getByRole("table").first().getByRole("row");
  await expect(rows.nth(1)).toContainText("All projects");
  await expect(rows.nth(1)).toContainText("Any date");
  await expect(rows.nth(1)).toContainText("$18.00");
  await expect(rows.nth(2)).toContainText("Nov 1, 2026");
  await expect(rows.nth(3)).toContainText("P1 · North Garage");
  await expect(rows.nth(3)).toContainText("$16.75");
  await expect(page.getByRole("row").filter({ hasText: "Rate set" })).toContainText("(Project P1 (North Garage))");
});

test("a project agreement from a date is saved with its scope, and a quote sheet carries it too", async ({ page }) => {
  const posts = [];
  await mock(page, posts);
  await page.goto("/pricing");
  const form = page.locator("form").filter({ has: page.getByRole("button", { name: "Save unit price" }) });
  await form.getByRole("combobox", { name: "Supplier" }).selectOption("1");
  await form.getByRole("combobox", { name: "Material" }).selectOption("2");
  await form.getByRole("combobox", { name: "Applies to" }).selectOption("8");
  await expect(form.getByRole("combobox", { name: "Applies to" })).toHaveValue("8");
  await form.getByRole("textbox", { name: "From (optional)" }).fill("2026-12-01");
  await page.getByLabel("Unit price ($/ton)").fill("16.40");
  await page.getByRole("button", { name: "Save unit price" }).click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0]).toEqual({ supplier_id: 1, material_id: 2, unit_price: "16.40", project_id: 8, effective_from: "2026-12-01", effective_to: null });

  await page.locator("#sheet-project").selectOption("8");
  await page.locator("#sheet-from").fill("2026-12-01");
  await page.getByRole("textbox", { name: "Rate sheet" }).fill("Example Aggregates\tNo. 53 Crushed Stone\t16.40");
  await page.getByRole("button", { name: "Check this sheet" }).click();
  await expect(page.getByTestId("sheet-summary")).toContainText("for Project P2 (Bridge Deck), from 2026-12-01");
  expect(posts[1]).toMatchObject({ apply: false, project_id: 8, effective_from: "2026-12-01" });
});

test("previewing a rate shows which waiting tickets it would reprice before anything is saved", async ({ page }) => {
  const posts = [];
  await mock(page, posts);
  await page.route("**/api/rates/preview", (route) => json(route, { scope: "Project P1 (North Garage)", tickets: 1, newly_priced: 0, cost_change: "-30.63",
    truncated: false, note: "Tickets not yet approved only.", rows: [{ ticket_id: 5, batch_id: 2, ticket_number: "10482", delivery_date: "2026-10-15",
      project: "P1", tons: "24.500", price_now: "18.0000", price_after: "16.7500", cost_change: "-30.63" }] }));
  await page.goto("/pricing");
  const form = page.locator("form").filter({ has: page.getByRole("button", { name: "Save unit price" }) });
  await form.getByRole("combobox", { name: "Supplier" }).selectOption("1");
  await form.getByRole("combobox", { name: "Material" }).selectOption("2");
  await form.getByRole("combobox", { name: "Applies to" }).selectOption("7");
  await page.getByLabel("Unit price ($/ton)").fill("16.75");
  await page.getByRole("button", { name: "Preview effect" }).click();
  const preview = page.getByTestId("rate-preview");
  await expect(preview).toContainText("1 ticket waiting for approval would change price");
  await expect(preview).toContainText("−$30.63");
  await expect(preview.getByRole("row").nth(1)).toContainText("$18.00");
  expect(posts).toEqual([]); // previewing saves nothing
});

test("the pricing timeline shows each scope's dated prices, its gaps and expiries, and what they block", async ({ page }) => {
  await mock(page, []);
  await page.goto("/pricing");
  const timeline = page.getByRole("region", { name: "Pricing timeline" });
  const pair = timeline.getByTestId("timeline-pair");
  await expect(pair).toContainText("2 waiting tickets can't be priced");
  await expect(pair).toContainText("$18.00 Jan 1, 2026 – Sep 30, 2026");
  await expect(pair).toContainText("no price from 2026-10-01 to 2026-11-30");
  await expect(pair).toContainText("The company rate is not used instead.");
  await page.locator("#rate-until").fill("2026-12-31");
  await expect(page.locator("#rate-until")).toHaveValue("2026-12-31");
});
