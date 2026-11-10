import { expect, test } from "@playwright/test";
import { profile } from "./accounts.mjs";

// Fictional records. The mock follows backend/app/routers/records.py.
const cors = {
  "access-control-allow-origin": `http://127.0.0.1:${process.env.TF_TEST_PORT ?? "3000"}`,
  "access-control-allow-credentials": "true",
  "access-control-allow-methods": "GET, POST, PATCH, DELETE, OPTIONS",
  "access-control-allow-headers": "content-type, x-ticketflow-request",
};
const json = (route, value, status = 200) => route.fulfill({ status, contentType: "application/json", headers: cors, body: JSON.stringify(value) });

const row = (id) => ({
  id, batch_id: 3, file_id: 1, source_page: 1, position: 0, ticket_number: String(10000 + id), delivery_date: "2026-10-15",
  material_type: "No. 57 Stone", tonnage: "20.500", supplier: "Example Aggregates", job_location: "Lot A", flags: [], status: "needs_review",
  tonnage_confirmed: false, error: null, unit_price_override: null, price_snapshot_state: "pending", unit_price: null, price_source: null,
  cost: null, duplicates: [], pricing_notes: [], verified_by: null, verified_at: null,
});

async function mock(page, { role = "owner", total = 120 } = {}) {
  const log = { searches: [], bulk: [], views: [], deleted: [] };
  const views = [{ id: 1, name: "October stone", filters: { material: "stone", date_from: "2026-10-01" }, columns: ["ticket_number", "tons"], created_by: "Fictional Owner", created_at: "2026-11-01T00:00:00Z" }];
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return json(route, profile(role));
    if (path === "/api/projects") return json(route, [{ id: 1, code: "24-115", name: "Fictional Road Job", job_location: null, description: null, status: "active" }]);
    if (path === "/api/suppliers") return json(route, [{ id: 1, name: "Example Aggregates", contact: null }]);
    if (path === "/api/materials") return json(route, []);
    if (path === "/api/saved-views" && request.method() === "POST") {
      log.views.push(request.postDataJSON());
      const view = { id: 2, created_by: "x", created_at: "2026-11-01T00:00:00Z", ...request.postDataJSON() };
      views.push(view);
      return json(route, view, 201);
    }
    if (path === "/api/saved-views") return json(route, views);
    if (/^\/api\/saved-views\/\d+$/.test(path)) { log.deleted.push(path); return route.fulfill({ status: 204, headers: cors }); }
    if (path === "/api/records/search") {
      log.searches.push(Object.fromEntries(url.searchParams));
      const pageNumber = Number(url.searchParams.get("page"));
      const size = Number(url.searchParams.get("page_size"));
      const start = (pageNumber - 1) * size;
      const rows = Array.from({ length: Math.max(0, Math.min(size, total - start)) }, (_, i) => row(start + i + 1));
      return json(route, { rows, total, page: pageNumber, page_size: size, summary: { records: total, by_status: { verified: 40, needs_review: 80 }, verified_tons: "820.125", verified_cost: "24603.75", verified_unpriced: 2 } });
    }
    if (path === "/api/tickets/bulk-edit") {
      log.bulk.push(request.postDataJSON());
      return json(route, { changed: 2, unchanged: 0, left_alone: { verified: 1 } });
    }
    return json(route, { detail: "Unmocked " + path }, 404);
  });
  return log;
}

test("the register reads one page at a time, and totals cover every matching record", async ({ page }) => {
  const log = await mock(page);
  await page.goto("/records");
  await expect(page.getByTestId("records-summary")).toContainText("120 records");
  await expect(page.getByTestId("records-summary")).toContainText("820.125 verified tons");
  await expect(page.getByTestId("records-summary")).toContainText("2 verified with no locked price");
  await expect(page.getByRole("row")).toHaveCount(51);
  await expect(page.getByText("Showing 1–50 of 120")).toBeVisible();
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.getByText("Showing 51–100 of 120")).toBeVisible();
  await expect(page.getByText("Page 2 of 3")).toBeVisible();
  expect(log.searches.at(-1).page).toBe("2");
});

test("sorting is done by the server and shown on the header", async ({ page }) => {
  const log = await mock(page);
  await page.goto("/records");
  await page.getByRole("button", { name: "Tons" }).click();
  await expect.poll(() => log.searches.at(-1)?.sort).toBe("tons");
  expect(log.searches.at(-1).direction).toBe("asc");
  await expect(page.getByRole("columnheader", { name: /Tons/ })).toHaveAttribute("aria-sort", "ascending");
  await page.getByRole("button", { name: /Tons/ }).click();
  await expect.poll(() => log.searches.at(-1)?.direction).toBe("desc");
});

test("search waits for typing to finish, goes back to page one, and survives a reload", async ({ page }) => {
  const log = await mock(page);
  await page.goto("/records");
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.getByText("Page 2 of 3")).toBeVisible();
  const before = log.searches.length;
  await page.getByLabel("Search", { exact: true }).pressSequentially("10482", { delay: 20 });
  await expect.poll(() => log.searches.at(-1)?.q).toBe("10482");
  expect(log.searches.length - before).toBeLessThanOrEqual(2);
  expect(log.searches.at(-1).page).toBe("1");
  await expect(page).toHaveURL(/q=10482/);
  await page.reload();
  await expect(page.getByLabel("Search", { exact: true })).toHaveValue("10482");
});

test("a bulk change names how many tickets it touches and reports what it left alone", async ({ page }) => {
  const log = await mock(page);
  await page.goto("/records");
  await page.getByLabel("Select ticket 10001").check();
  await page.getByLabel("Select ticket 10002").check();
  await page.getByLabel("Select ticket 10003").check();
  const bar = page.getByRole("group", { name: "Change selected tickets" });
  await expect(bar).toContainText("3 selected");
  await expect(bar).toContainText("Tons and ticket numbers can only be changed one ticket at a time");
  // Only names and dates are offered.
  await expect(bar.getByLabel("Set").locator("option")).toHaveText(["Supplier", "Material", "Job location", "Delivery date"]);
  const apply = bar.getByRole("button", { name: "Apply to 3 tickets" });
  await expect(apply).toBeDisabled();
  await bar.getByLabel("To").fill("Example Aggregates");
  await apply.click();
  await expect(page.getByRole("status").filter({ hasText: "Changed 2." })).toContainText("Left alone: 1 verified");
  expect(log.bulk).toEqual([{ ticket_ids: [1, 2, 3], field: "supplier", value: "Example Aggregates" }]);
  await expect(bar).toHaveCount(0);
});

test("select all covers this page only", async ({ page }) => {
  await mock(page);
  await page.goto("/records");
  await page.getByLabel("Select all on this page").check();
  await expect(page.getByRole("group", { name: "Change selected tickets" })).toContainText("50 selected");
});

test("viewers get no selection or bulk tools", async ({ page }) => {
  await mock(page, { role: "viewer" });
  await page.goto("/records");
  await expect(page.getByRole("row")).toHaveCount(51);
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Name cleanup" })).toHaveCount(0);
});

test("a saved view restores its filters and its export columns", async ({ page }) => {
  const log = await mock(page);
  await page.goto("/records");
  await page.getByLabel("Saved view").selectOption({ label: "October stone" });
  await expect(page.getByLabel("Material", { exact: true })).toHaveValue("stone");
  await expect.poll(() => log.searches.at(-1)?.material).toBe("stone");
  const csv = page.getByRole("link", { name: "Export CSV" });
  await expect(csv).toHaveAttribute("href", /export\.csv\?.*material=stone.*columns=ticket_number,tons$/);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Remove view" }).click();
  await expect.poll(() => log.deleted).toEqual(["/api/saved-views/1"]);
});

test("export columns can be chosen and saved with the view", async ({ page }) => {
  const log = await mock(page);
  await page.goto("/records");
  await page.getByRole("button", { name: "Choose columns" }).click();
  const dialog = page.getByRole("dialog", { name: "Export columns" });
  await dialog.getByLabel("Project").check();
  await dialog.getByLabel("Source file").uncheck();
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(page.getByRole("link", { name: "Export CSV" })).toHaveAttribute("href", /columns=.*status,approved_by,approved_at,project$/);
  await page.getByRole("button", { name: "Save this view" }).click();
  const save = page.getByRole("dialog", { name: "Save this view" });
  await save.getByLabel("Name").fill("With project");
  await save.getByRole("button", { name: "Save view" }).click();
  await expect.poll(() => log.views.length).toBe(1);
  expect(log.views[0].name).toBe("With project");
  expect(log.views[0].columns).toContain("project");
  expect(log.views[0].columns).not.toContain("source");
});

test("name cleanup explains each spelling and only changes what someone applies", async ({ page }) => {
  const posted = [];
  const aliases = [];
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return json(route, profile("manager"));
    if (path === "/api/suppliers") return json(route, [{ id: 4, name: "Example Aggregates", contact: null }]);
    if (path === "/api/name-aliases") { aliases.push(request.postDataJSON()); return json(route, { id: 1 }, 201); }
    if (path === "/api/records/rename") { posted.push(request.postDataJSON()); return json(route, { changed: 12, unchanged: 0, left_alone: { verified: 3 } }); }
    if (path === "/api/records/name-variants") return json(route, { field: "supplier", catalog: ["Example Aggregates"], rows: [
      { value: "Examle Agregates", state: "unknown", target: "Example Aggregates", tickets: 15, verified: 3, unverified: 12 },
      { value: "Example Aggregates", state: "catalog", target: "Example Aggregates", tickets: 40, verified: 40, unverified: 0 },
    ] });
    return json(route, { detail: "Unmocked " + path }, 404);
  });
  await page.goto("/records/names");
  const typo = page.getByRole("row").filter({ hasText: "Examle Agregates" });
  await expect(typo).toContainText("Not in your list");
  await expect(typo.getByRole("combobox")).toHaveValue("Example Aggregates"); // a suggestion, not yet applied
  expect(posted).toEqual([]);
  await expect(page.getByRole("row").filter({ hasText: /^Example Aggregates/ })).toContainText("Nothing to do");
  await typo.getByLabel("Remember spelling").check();
  await typo.getByRole("button", { name: "Apply to 12 unapproved" }).click();
  await expect(page.getByRole("status")).toContainText("3 approved ticket(s) keep their spelling");
  expect(posted).toEqual([{ field: "supplier", from_value: "Examle Agregates", to_value: "Example Aggregates" }]);
  expect(aliases).toEqual([{ kind: "supplier", alias: "Examle Agregates", target_id: 4 }]);
});

test("the project report separates approved from unapproved and marks unknown figures", async ({ page }) => {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return json(route, profile("viewer"));
    if (path === "/api/projects") return json(route, [{ id: 1, code: "24-115", name: "Fictional Road Job", job_location: null, description: null, status: "active" }]);
    if (path === "/api/reports/project/1") {
      const group = { tickets: 10, verified_tickets: 8, verified_tons: "164.000", cost: "4920.00", unpriced: 1, pending_tickets: 2, pending_tons: "41.000" };
      return json(route, {
        project: { id: 1, code: "24-115", name: "Fictional Road Job", status: "active" }, first_delivery: "2026-10-01", last_delivery: "2026-10-31",
        totals: group, by_supplier_material: [{ supplier: "Example Aggregates", material: "No. 57 Stone", ...group }], by_month: [{ month: "2026-10", ...group }],
        invoices: { billed_total: "5000.00", verified_total: "0", lines_unresolved: 1, by_status: { incomplete: 1 }, lines: [{
          id: 9, invoice_number: "EA-1107", supplier: "Example Aggregates", material: "No. 57 Stone", date_from: "2026-10-01", date_to: "2026-10-31",
          status: "incomplete", comparison_mode: "itemized", billed_tons: "164.000", verified_tons: "164.000", tons_difference: "0.000",
          billed_subtotal: "5000.00", verified_subtotal: null, subtotal_difference: null }] },
      });
    }
    return json(route, { detail: "Unmocked " + path }, 404);
  });
  await page.goto("/reports/project?project_id=1");
  const totals = page.getByRole("region", { name: "Project totals" });
  await expect(totals).toContainText("164");
  await expect(totals).toContainText("1 approved with no locked price");
  await expect(totals).toContainText("41.00 t");
  await expect(page.getByText("Not an approval for payment.")).toBeVisible();
  const lines = page.getByRole("region", { name: "Invoice lines" });
  await expect(lines.getByRole("row").filter({ hasText: "EA-1107" })).toContainText("Unresolved");
  await expect(lines).toContainText("1 line(s) with an unresolved figure are left out");
  await expect(page.getByRole("link", { name: "Download Excel" })).toHaveAttribute("href", /\/api\/reports\/project\/1\/report\.xlsx$/);
});
