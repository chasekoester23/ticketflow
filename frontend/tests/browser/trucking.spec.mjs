import { expect, test } from "@playwright/test";
import { profile } from "./accounts.mjs";

const cors = {
  "access-control-allow-origin": `http://127.0.0.1:${process.env.TF_TEST_PORT ?? "3000"}`, "access-control-allow-credentials": "true",
  "access-control-allow-methods": "GET, POST, PUT, DELETE, OPTIONS", "access-control-allow-headers": "content-type, x-ticketflow-request, if-match",
};
const json = (route, value, status = 200) => route.fulfill({ status, contentType: "application/json", headers: cors, body: JSON.stringify(value) });

const SHIFT = {
  id: 5, revision: "1", status: "needs_review", project_id: 7, project: "P1", work_date: "2026-11-03", truck_id: 3, truck: "T-12",
  equipment_type: "quad_axle", hauler_id: 2, hauler: "Fictional Hauling LLC", driver_name: "R. Diaz",
  start_at: "2026-11-03T22:00", end_at: "2026-11-04T04:30", unpaid_break_minutes: 0, standby_hours: "1.50", claimed_hours: "7.00",
  loads: 3, field_signoff: null, cost_code: "31-2300", notes: null, on_site_hours: "6.50", work_hours: "5.00",
  rate: { id: 1, hourly_rate: "120.0000", standby_rate: "90.0000", minimum_hours: "8.00" },
  cost_lines: [{ label: "Working hours", hours: "5.00", rate: "120.0000", amount: "600.00" }, { label: "Standby", hours: "1.50", rate: "90.0000", amount: "135.00" },
    { label: "Minimum charge (8.00 h)", hours: "1.50", rate: "120.0000", amount: "180.00" }],
  cost: "915.00", cost_is_locked: false, approved: null,
  flags: ["The hauler claims 7.00 h; the time sheet's times give 6.50 h on site.", "No field sign-off recorded from the time sheet."],
  deliveries: { count: 0, tons: null, ticket_ids: [], cost_per_ton: null },
};
const PAGE = (shifts) => ({ shifts, truncated: false, summary: { shifts: shifts.length, awaiting_approval: 1, disputed: 0, approved_hours: "18.00",
  approved_cost: "1980.00", loads: 7, with_flags: 1 } });

const INVOICE = { id: 9, revision: "1", hauler_id: 2, hauler: "Fictional Hauling LLC", project_id: 7, project: "P1", invoice_number: "FH-1001",
  invoice_date: null, period_from: "2026-11-01", period_to: "2026-11-30", document: null, billed: "1455.00", supported: "990.00", difference: "465.00", exceptions: 2,
  lines: [{ line_no: 1, kind: "hours", truck_identifier: "T-12", work_date: "2026-11-03", hours: "10.00", rate: "115.0000", amount: "1150.00", description: null,
    result: "differs", shift_id: 5, supported: "990.00", difference: "160.00" },
    { line_no: 2, kind: "fee", truck_identifier: null, work_date: null, hours: null, rate: null, amount: "305.00", description: "Fuel surcharge",
      result: "fee", shift_id: null, supported: "0.00", difference: "305.00" }] };

async function mock(page, role, { onApprove, requests = [], posted = [] } = {}) {
  let current = SHIFT;  // what the server holds, so a reload after approving shows it approved
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (url.pathname === "/api/auth/me") return json(route, profile(role));
    if (url.pathname === "/api/projects") return json(route, [{ id: 7, code: "P1", name: "Fictional Sitework", status: "active", job_location: null, description: null }]);
    if (url.pathname === "/api/haulers") return json(route, [{ id: 2, name: "Fictional Hauling LLC", contact: null, trucks: [{ id: 3, identifier: "T-12", equipment_type: "quad_axle", active: true }] }]);
    if (url.pathname === "/api/hauling-rates") return json(route, []);
    if (url.pathname === "/api/hauling-invoices" && request.method() === "POST") { posted.push(request.postDataJSON()); return json(route, INVOICE, 201); }
    if (url.pathname === "/api/hauling-invoices") return json(route, [INVOICE]);
    if (url.pathname === "/api/truck-shifts/5/dispute") { posted.push({ dispute: request.postDataJSON(), ifMatch: request.headers()["if-match"] });
      return json(route, { ...current, status: "disputed", revision: "2" }); }
    if (url.pathname === "/api/truck-shifts") { requests.push(url.search); return json(route, PAGE([current])); }
    if (url.pathname === "/api/truck-shifts/5/history") return json(route, []);
    if (url.pathname === "/api/truck-shifts/5/approve") { current = onApprove(request); return json(route, current); }
    return json(route, { detail: "Unmocked " + url.pathname }, 404);
  });
}

test("a reviewer sees what a time sheet works out to, what doesn't add up, and approves the hours they state", async ({ page }) => {
  const posts = [];
  await mock(page, "manager", { onApprove: (request) => {
    posts.push({ body: request.postDataJSON(), ifMatch: request.headers()["if-match"] });
    return { ...SHIFT, status: "approved", cost_is_locked: true, revision: "2",
      approved: { work_hours: "5.00", standby_hours: "1.50", billable_hours: "8.00", hourly_rate: "120.0000", standby_rate: "90.0000", minimum_hours: "8.00", by: "Pat Manager", at: "2026-11-04T12:00:00Z" } };
  } });
  await page.goto("/trucking");
  const totals = page.getByRole("region", { name: "Trucking totals" });
  await expect(totals).toContainText("$1,980.00");
  const row = page.getByRole("table", { name: "Shift list" }).getByRole("row").nth(1);
  await expect(row).toContainText("2 to check");
  await expect(row).toContainText("if approved");
  await row.click();
  const detail = page.getByTestId("shift-detail");
  await expect(detail).toContainText("(next day)");
  await expect(detail.getByTestId("shift-flags")).toContainText("claims 7.00 h");
  await expect(detail.getByRole("table", { name: "Cost" })).toContainText("Minimum charge (8.00 h)");
  await expect(detail.getByLabel("Approved working hours")).toHaveValue("5.00");  // the sheet's figures, as a starting point
  await detail.getByRole("button", { name: /^Approve/ }).click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0]).toEqual({ body: { work_hours: "5.00", standby_hours: "1.50" }, ifMatch: "1" });
  await expect(detail.getByTestId("shift-approved")).toContainText("Locked");
});

test("a viewer can't approve, and the overview's link opens shifts awaiting approval", async ({ page }) => {
  const requests = [];
  await mock(page, "viewer", { requests });
  await page.goto("/trucking?status=needs_review");
  await expect.poll(() => requests.at(-1)).toBe("?status=needs_review");
  await page.getByRole("table", { name: "Shift list" }).getByRole("row").nth(1).click();
  await expect(page.getByTestId("shift-detail")).toBeVisible();
  await expect(page.getByRole("button", { name: /^Approve/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "+ Enter a shift" })).toHaveCount(0);
});

test("a hauling invoice shows billed against supported, line by line, and a new one is entered with its lines", async ({ page }) => {
  const posted = [];
  await mock(page, "manager", { posted });
  await page.goto("/trucking");
  const invoices = page.getByRole("region", { name: "Hauling invoices" });
  const row = invoices.getByRole("table", { name: "Hauling invoice list" }).getByRole("row").nth(1);
  await expect(row).toContainText("$1,455.00");
  await expect(row).toContainText("$465.00");
  await row.getByRole("button", { name: "FH-1001" }).click();
  const lines = invoices.getByRole("table", { name: "Lines of FH-1001" });
  await expect(lines.getByRole("row").nth(0)).toContainText("Differs");
  await expect(lines.getByRole("row").nth(0)).toContainText("+$160.00");
  await expect(lines.getByRole("row").nth(1)).toContainText("Fee: not in an agreement");

  await invoices.getByRole("button", { name: "+ Enter an invoice" }).click();
  const form = page.getByRole("form", { name: "Enter hauling invoice" });
  await form.getByLabel("Hauler").selectOption("2");
  await form.getByLabel("Invoice number").fill("FH-1002");
  await form.getByLabel("Period from").fill("2026-11-01");
  await form.getByLabel("Period to").fill("2026-11-30");
  await form.getByLabel("Truck").fill("T-12");
  await form.getByLabel("Date worked").fill("2026-11-04");
  await form.getByLabel("Hours", { exact: true }).fill("9");
  await form.getByLabel("Rate", { exact: true }).fill("110");
  await form.getByLabel("Amount").fill("990.00");
  await form.getByRole("button", { name: "Save and check" }).click();
  await expect.poll(() => posted.length).toBe(1);
  expect(posted[0]).toMatchObject({ hauler_id: 2, invoice_number: "FH-1002", period_from: "2026-11-01",
    lines: [{ kind: "hours", truck_identifier: "T-12", work_date: "2026-11-04", hours: "9", rate: "110", amount: "990.00" }] });
});

test("a manager disputes a shift with a reason, and approval waits until it's settled", async ({ page }) => {
  const posted = [];
  await mock(page, "manager", { posted, onApprove: () => SHIFT });
  await page.goto("/trucking?shift_id=5");
  const detail = page.getByTestId("shift-detail");
  await detail.getByLabel("Reason to dispute with the hauler").fill("Hauler bills 7 h; the sheet shows 6.5");
  await detail.getByRole("button", { name: "Dispute" }).click();
  await expect.poll(() => posted.length).toBe(1);
  expect(posted[0]).toEqual({ dispute: { status: "disputed", reason: "Hauler bills 7 h; the sheet shows 6.5" }, ifMatch: "1" });
});
