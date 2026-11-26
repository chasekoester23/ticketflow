import { expect, test } from "@playwright/test";
import { profile } from "./accounts.mjs";

// Fictional data. The mock follows backend/app/routers/delivery_imports.py.
const cors = {
  "access-control-allow-origin": `http://127.0.0.1:${process.env.TF_TEST_PORT ?? "3000"}`, "access-control-allow-credentials": "true",
  "access-control-allow-methods": "GET, POST, PUT, PATCH, OPTIONS", "access-control-allow-headers": "content-type, if-match, x-ticketflow-request",
};
const json = (route, value, status = 200) => route.fulfill({ status, contentType: "application/json", headers: cors, body: JSON.stringify(value) });
const PIXEL = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4//8/AwAI/AL+0I0YAAAAAElFTkSuQmCC", "base64");

const PROJECTS = [{ id: 7, code: "P1", name: "North Garage", job_location: "North Garage site", description: null, status: "active" }];
const FIELDS = [["ticket_number", "Ticket number", true], ["delivery_date", "Date", false], ["material_type", "Material", false],
  ["quantity", "Quantity", true], ["unit", "Unit", false], ["supplier", "Supplier", false], ["job_location", "Job", false],
  ["truck_number", "Truck", false]].map(([key, label, required]) => ({ key, label, required }));
const COLUMNS = ["Ticket #", "Date", "Product", "Net Lbs", "Truck"].map((header, index) => ({ index, header }));
const BASE = {
  id: 12, original_name: "example-aggregates-nov.csv", file_kind: "csv", header_row: 3, rows: 4, project_id: null, project_code: null,
  project_name: null, mapping: null, defaults: null, revision: "rev-1", created_at: "2026-11-25T15:00:00Z", committed_at: null,
  batch_id: null, result: null, fields: FIELDS, limits: { bytes: 10485760, rows: 2000, columns: 60 }, columns: COLUMNS,
  suggested: { mapping: { ticket_number: 0, delivery_date: 1, material_type: 2, quantity: 3, truck_number: 4 }, unit: "lb" },
  sample: [["10482", "11/03/2026", "No. 57 Stone", "44300", "T-12"]],
  header_candidates: [{ row: 1, cells: ["Example Aggregates delivery report"] }, { row: 3, cells: COLUMNS.map((c) => c.header) }],
};
const row = (n, fields) => ({ row: n, action: "import", errors: [], warnings: [], duplicate: null, ticket_number: null, delivery_date: "2026-11-03",
  material_type: "No. 57 Stone", quantity: "22.150", unit: "ton", source_quantity: "44300.000", source_unit: "lb",
  supplier: "Example Aggregates", job_location: "North Garage site", truck_number: "T-12", ...fields });
const PREVIEW = {
  rows: [
    row(4, { ticket_number: "10482" }),
    row(5, { ticket_number: "10483", action: "duplicate", duplicate: { kind: "existing", ticket_id: 90, batch_id: 4, status: "verified", project_code: "P1",
      message: "Example Aggregates ticket 10483 is already on record (batch 4, approved)." } }),
    row(6, { ticket_number: "10484", action: "problem", quantity: null, source_quantity: null, source_unit: null,
      errors: ["Quantity is a formula (=2*10). Quarvera never calculates spreadsheet formulas: export the values, or enter this one on the review screen."] }),
    row(7, { ticket_number: "10482", action: "duplicate", duplicate: { kind: "file", row: 4, message: "Same supplier and ticket number as row 4 of this file." } }),
  ],
  counts: { rows: 4, ready: 1, problems: 1, duplicates_in_file: 1, duplicates_existing: 1, warnings: 0 },
};

async function mock(page, log, { role = "owner" } = {}) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return json(route, profile(role));
    if (path === "/api/status") return json(route, { extraction_configured: true, model: "stand-in" });
    if (path === "/api/projects") return json(route, PROJECTS);
    if (path === "/api/suppliers" || path === "/api/materials" || path === "/api/haulers") return json(route, []);
    if (path === "/api/batches/queue") return json(route, []);
    if (path === "/api/delivery-imports" && method === "POST") { log.uploaded.push(request.postData() || ""); return json(route, BASE, 201); }
    if (path === "/api/delivery-imports") return json(route, log.history);
    if (path === "/api/delivery-imports/12/mapping") {
      const body = request.postDataJSON();
      log.mapped.push(body);
      return json(route, { ...BASE, status: "mapped", mapping: body.columns, project_id: body.project_id, project_code: "P1",
        project_name: "North Garage", defaults: { supplier: body.supplier, unit: body.unit, job_location: body.job_location }, preview: PREVIEW });
    }
    if (path === "/api/delivery-imports/12/commit") {
      log.committed.push(request.postDataJSON());
      return json(route, { ...BASE, status: "imported", batch_id: 31, columns: undefined, committed_at: "2026-11-25T15:05:00Z",
        result: { imported: 2, skipped_duplicates: 1, error_rows: 1, batch_id: 31 } });
    }
    if (path === "/api/batches/31") return json(route, { id: 31, project_id: 7, layout: "single", intake_mode: "import", status: "ready_for_review",
      created_at: "2026-11-25T15:05:00Z", expected_ticket_count: 2, files: [{ id: 40, original_name: "example-aggregates-nov.csv", status: "ready_for_review", error: null }] });
    if (path === "/api/batches/31/tickets") return json(route, [{
      id: 501, batch_id: 31, file_id: 40, source_page: 4, position: 0, ticket_number: "10482", delivery_date: "2026-11-03", material_type: "No. 57 Stone",
      tonnage: "22.150", unit: "ton", source_quantity: "44300.000", source_unit: "lb", supplier: "Example Aggregates", job_location: "North Garage site",
      flags: [], status: "needs_review", tonnage_confirmed: false, error: null, unit_price_override: null, price_snapshot_state: "pending",
      unit_price: null, price_source: null, cost: null, duplicates: [], pricing_notes: ["No rate"], revision: "r501" }]);
    if (path === "/api/batches/31/manifest") return json(route, { detail: "n/a" }, 404);
    if (/\/image$/.test(path)) return route.fulfill({ contentType: "image/png", headers: cors, body: PIXEL });
    return json(route, { detail: "Unmocked " + path }, 404);
  });
}

const newLog = () => ({ uploaded: [], mapped: [], committed: [], history: [] });

test("the upload screen leads to the spreadsheet import", async ({ page }) => {
  await mock(page, newLog());
  await page.goto("/upload");
  await page.getByRole("link", { name: "Import a spreadsheet" }).click();
  await expect(page).toHaveURL(/\/upload\/import$/);
  await expect(page.getByRole("heading", { name: "Import a delivery spreadsheet" })).toBeVisible();
});

test("a supplier's file is mapped, previewed with its problems and duplicates, and imported for review", async ({ page }) => {
  const log = newLog();
  await mock(page, log);
  await page.goto("/upload/import");
  await page.getByTestId("import-file").setInputFiles({ name: "example-aggregates-nov.csv", mimeType: "text/csv",
    buffer: Buffer.from("Example Aggregates delivery report\n\nTicket #,Date,Product,Net Lbs,Truck\n10482,11/03/2026,No. 57 Stone,44300,T-12\n") });
  await expect.poll(() => log.uploaded.length).toBe(1);
  expect(log.uploaded[0]).toContain('filename="example-aggregates-nov.csv"');

  // Suggested columns, ready to check.
  await expect(page.getByRole("combobox", { name: "Column for Ticket number" })).toHaveValue("0");
  await expect(page.getByRole("combobox", { name: "Column for Quantity" })).toHaveValue("3");
  await expect(page.getByRole("combobox", { name: "Column for Unit" })).toHaveValue("");
  await expect(page.getByRole("combobox", { name: "Unit" , exact: true })).toHaveValue("lb");
  await expect(page.getByRole("table", { name: "First rows of the file" })).toContainText("44300");
  await page.getByRole("combobox", { name: "Project" }).selectOption("7");
  await page.getByRole("textbox", { name: "Supplier" }).fill("Example Aggregates");
  await page.getByRole("button", { name: "Use “North Garage site”" }).click();
  await page.getByRole("button", { name: "Preview the rows" }).click();
  await expect.poll(() => log.mapped.length).toBe(1);
  expect(log.mapped[0]).toEqual({ header_row: 3, columns: { ticket_number: 0, delivery_date: 1, material_type: 2, quantity: 3, truck_number: 4 },
    project_id: 7, supplier: "Example Aggregates", unit: "lb", job_location: "North Garage site" });

  // The preview: pounds shown as tons, a formula is a problem, duplicates skipped by default.
  const table = page.getByRole("table", { name: "Rows to import" });
  await expect(page.getByTestId("row-4")).toContainText("22.15 t (44,300 lb)");
  await expect(page.getByTestId("row-6")).toContainText("Quantity is a formula");
  await expect(page.getByTestId("row-5")).toContainText("already on record");
  await expect(page.getByTestId("row-7")).toContainText("Same supplier and ticket number as row 4");
  await expect(table.getByRole("combobox", { name: "Row 5 duplicate" })).toHaveValue("skip");
  await expect(page.getByRole("button", { name: "Import 1 ticket for review" })).toBeVisible();
  await table.getByRole("combobox", { name: "Row 5 duplicate" }).selectOption("import");
  await page.getByRole("button", { name: "1 with problems" }).click();
  await expect(page.getByTestId("row-4")).toHaveCount(0);
  await expect(page.getByTestId("row-6")).toBeVisible();
  await page.getByRole("button", { name: "Import 2 tickets for review" }).click();
  await expect.poll(() => log.committed.length).toBe(1);
  expect(log.committed[0]).toEqual({ duplicates: { 5: "import" }, include_problem_rows: false, revision: "rev-1" });

  const done = page.getByTestId("import-done");
  await expect(done).toContainText("2 tickets imported from example-aggregates-nov.csv, waiting for review");
  await expect(done).toContainText("1 duplicate skipped");
  await expect(done.getByRole("link", { name: "Review the imported tickets" })).toHaveAttribute("href", "/upload?batch=31");
});

test("an imported ticket shows the row it came from where a scan would be", async ({ page }) => {
  const log = newLog();
  log.history = [{ id: 12, original_name: "example-aggregates-nov.csv", file_kind: "csv", status: "imported", rows: 4, imported: 2,
    skipped_duplicates: 1, error_rows: 1, batch_id: 31, project_id: 7, project_code: "P1", created_at: "2026-11-25T15:00:00Z",
    created_by: "Pat Owner", committed_at: "2026-11-25T15:05:00Z", committed_by: "Pat Owner" }];
  await mock(page, log);
  await page.goto("/upload?batch=31");
  const shown = page.getByTestId("imported-row");
  await expect(shown).toContainText("Imported from example-aggregates-nov.csv, row 4");
  await expect(shown.getByRole("img", { name: "Row 4 of example-aggregates-nov.csv, as the file has it" })).toBeVisible();
  await expect(shown.getByRole("link", { name: "Download the original file" })).toHaveAttribute("href", /\/api\/delivery-imports\/12\/file$/);
  await expect(page.getByTestId("scan-box")).toHaveCount(0);
  await expect(page.getByText("Spreadsheet import")).toBeVisible();
});

test("past imports list their counts and the original file", async ({ page }) => {
  const log = newLog();
  log.history = [{ id: 12, original_name: "example-aggregates-nov.csv", file_kind: "csv", status: "imported", rows: 4, imported: 2,
    skipped_duplicates: 1, error_rows: 1, batch_id: 31, project_id: 7, project_code: "P1", created_at: "2026-11-25T15:00:00Z",
    created_by: "Pat Owner", committed_at: "2026-11-25T15:05:00Z", committed_by: "Pat Owner" }];
  await mock(page, log);
  await page.goto("/upload/import");
  const history = page.getByRole("region", { name: "Import history" });
  const line = history.getByRole("row").filter({ hasText: "example-aggregates-nov.csv" });
  await expect(line).toContainText("P1");
  await expect(line.getByRole("cell").nth(4)).toHaveText("2");
  await expect(line.getByRole("cell").nth(5)).toHaveText("1");
  await expect(line.getByRole("link", { name: "Review tickets" })).toHaveAttribute("href", "/upload?batch=31");
  await expect(line.getByRole("link", { name: "example-aggregates-nov.csv" })).toHaveAttribute("href", /\/api\/delivery-imports\/12\/file$/);
});
