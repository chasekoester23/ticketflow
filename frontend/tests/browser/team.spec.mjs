import { expect, test } from "@playwright/test";
import { profile } from "./accounts.mjs";

const cors = {
  "access-control-allow-origin": "http://127.0.0.1:3000",
  "access-control-allow-credentials": "true",
  "access-control-allow-headers": "content-type, x-ticketflow-request",
  "access-control-allow-methods": "GET, POST, PATCH, OPTIONS",
};
const json = (route, value, status = 200) => route.fulfill({
  status, contentType: "application/json", headers: cors, body: JSON.stringify(value),
});
const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4//8/AwAI/AL+0I0YAAAAAElFTkSuQmCC",
  "base64",
);

function ticket(overrides = {}) {
  return {
    id: 101, batch_id: 5, file_id: 1, source_page: 1, position: 0,
    ticket_number: "10482", delivery_date: "2026-10-15", material_type: "No. 53 Stone",
    tonnage: "24.500", supplier: "Example Aggregates", job_location: "Project A",
    flags: [], status: "needs_review", tonnage_confirmed: false, error: null,
    unit_price_override: null, price_snapshot_state: "pending", unit_price: "30.0000",
    price_source: "rate", cost: null, duplicates: [], pricing_notes: [],
    verified_by: null, verified_at: null, ...overrides,
  };
}

async function mockReview(page, me, tickets, log = { calls: [] }) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    log.calls.push(`${method} ${path}`);
    if (path === "/api/auth/me") return json(route, me);
    if (path === "/api/status") return json(route, { extraction_configured: true, model: "test-only" });
    if (path === "/api/projects") return json(route, [{ id: 1, code: "P-1", name: "Project A", job_location: null, description: null, status: "active" }]);
    if (path === "/api/batches/5") return json(route, {
      id: 5, project_id: 1, layout: "grid_2x2", status: "ready_for_review", intake_mode: "manual",
      created_at: "2026-10-15T11:00:00Z", files: [{ id: 1, original_name: "scan.pdf", status: "ready_for_review", error: null }],
    });
    if (path === "/api/batches/5/tickets") return json(route, tickets);
    if (/\/image$/.test(path)) return route.fulfill({ contentType: "image/png", body: PNG });
    if (path === "/api/tickets/101/audit") return json(route, [
      { field: "status", old: "needs_review", new: "verified", at: "2026-10-16T15:04:00Z", actor: "Mia Manager" },
      { field: "supplier", old: null, new: "Example Aggregates", at: "2026-10-01T15:04:00Z", actor: null },
    ]);
    if (path === "/api/tickets/101" && method === "PATCH") {
      Object.assign(tickets[0], request.postDataJSON());
      return json(route, tickets[0]);
    }
    return json(route, { detail: "Not found" }, 404);
  });
}

test("clerk can enter and save tickets but cannot approve, skip, or change price", async ({ page }) => {
  const tickets = [ticket()];
  const log = { calls: [] };
  await mockReview(page, profile("clerk", { username: "cal", display_name: "Cal Clerk" }), tickets, log);
  await page.goto("/upload?batch=5&ticket=101");

  await expect(page.getByLabel("Ticket number")).toHaveValue("10482");
  await expect(page.getByRole("button", { name: "Accept & next" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Skip non-ticket" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Flag for later" })).toBeVisible();
  await expect(page.getByLabel(/Override rate/)).toHaveAttribute("readonly", "");
  await expect(page.getByText("Only a manager or owner can change a ticket’s rate.")).toBeVisible();
  await expect(page.getByRole("link", { name: "Team", exact: true })).toHaveCount(0);
  await expect(page.getByText("Cal Clerk")).toBeVisible();

  await page.getByLabel("Job location").fill("North gate");
  await page.getByRole("button", { name: "Save & next" }).click();
  await expect.poll(() => log.calls.filter((c) => c === "PATCH /api/tickets/101").length).toBe(1);
  expect(log.calls.some((c) => c.endsWith("/verify"))).toBe(false);
});

test("viewer gets a read-only review screen with no actions", async ({ page }) => {
  await mockReview(page, profile("viewer"), [ticket()]);
  await page.goto("/upload?batch=5&ticket=101");
  await expect(page.getByLabel("Ticket number")).toHaveAttribute("readonly", "");
  await expect(page.getByRole("button", { name: /Save/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Flag for later" })).toHaveCount(0);
  await expect(page.getByText("Your role is view-only.", { exact: false })).toBeVisible();
});

test("approved ticket names its approver and history marks pre-account changes", async ({ page }) => {
  await mockReview(page, profile("manager"), [ticket({
    status: "verified", tonnage_confirmed: true, price_snapshot_state: "priced", cost: "735.00",
    verified_by: "Mia Manager", verified_at: "2026-10-16T15:04:00Z",
  })]);
  await page.goto("/upload?batch=5&ticket=101");
  await expect(page.getByTestId("verified-by")).toContainText("Approved by Mia Manager");
  await page.getByText("Change history").click();
  await expect(page.getByText("Before individual accounts", { exact: false })).toBeVisible();
  await expect(page.getByText("Mia Manager ·", { exact: false })).toBeVisible();
});

test("temporary password forces the account page, then continues", async ({ page }) => {
  let me = profile("clerk", { username: "dana", must_change_password: true });
  const posted = [];
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return json(route, me);
    if (path === "/api/auth/password") {
      posted.push(request.postDataJSON());
      me = { ...me, must_change_password: false };
      return json(route, me);
    }
    if (path === "/api/dashboard") return json(route, {
      tickets: 0, needs_review: 0, flagged: 0, verified: 0, total_tons: 0, pending_tons: 0,
      total_cost: 0, unpriced_tickets: 0, pending_cost_tickets: 0, by_supplier: [], by_material: [], recent_batches: [],
    });
    if (path === "/api/projects") return json(route, []);
    return json(route, { detail: { code: "password_change_required", message: "Set a new password before continuing." } }, 403);
  });

  await page.goto("/records");
  await expect(page).toHaveURL(/\/account$/);
  await expect(page.getByText("Set your own password to continue.")).toBeVisible();
  await expect(page.getByRole("link", { name: "Delivery records" })).toHaveCount(0);

  const submit = page.getByRole("button", { name: "Set password and continue" });
  await page.getByLabel("Temporary password").fill("temporary-from-owner");
  await page.getByLabel("New password", { exact: true }).fill("short");
  await expect(page.getByText("7 more characters needed")).toBeVisible();
  await expect(submit).toBeDisabled();
  await page.getByLabel("New password", { exact: true }).fill("my own long passphrase");
  await page.getByLabel("Confirm new password").fill("my own long passphrase");
  await submit.click();
  await expect(page).toHaveURL(/\/$/);
  expect(posted).toEqual([{ current_password: "temporary-from-owner", new_password: "my own long passphrase" }]);
});

test("owner adds a person, sees the one-time password, and deactivates with confirmation", async ({ page }) => {
  const members = [{
    id: 1, username: "owner", display_name: "Pat Boss", role: "owner", role_label: "Owner",
    active: true, must_change_password: false, created_at: null, is_you: true,
  }];
  const requests = [];
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return json(route, profile("owner", { display_name: "Pat Boss" }));
    if (path === "/api/team/events") return json(route, []);
    if (path === "/api/team/members" && method === "GET") return json(route, members);
    if (path === "/api/team/members" && method === "POST") {
      const body = request.postDataJSON();
      requests.push(["add", body]);
      const member = {
        id: 2, username: body.username, display_name: body.display_name ?? null, role: body.role,
        role_label: "Clerk", active: true, must_change_password: true, created_at: "2026-11-03T12:00:00Z", is_you: false,
      };
      members.push(member);
      return json(route, { member, temporary_password: "Tmp-Example-Only-123" }, 201);
    }
    if (path === "/api/team/members/2" && method === "PATCH") {
      const body = request.postDataJSON();
      requests.push(["patch", body]);
      Object.assign(members[1], body);
      return json(route, members[1]);
    }
    return json(route, { detail: "Not found" }, 404);
  });

  await page.goto("/team");
  await expect(page.getByRole("link", { name: "Team", exact: true })).toHaveAttribute("aria-current", "page");
  await page.getByLabel("Full name").fill("Maria Lopez");
  await page.getByLabel(/^Username/).fill("mlopez");
  await page.getByRole("button", { name: "+ Add person" }).click();
  await expect(page.getByText("Tmp-Example-Only-123")).toBeVisible();
  await expect(page.getByText("It is shown only once.")).toBeVisible();
  expect(requests[0]).toEqual(["add", { username: "mlopez", role: "clerk", display_name: "Maria Lopez" }]);
  await expect(page.getByText("Awaiting first sign-in")).toBeVisible();
  await page.getByRole("button", { name: "Done — hide it" }).click();
  await expect(page.getByText("Tmp-Example-Only-123")).toHaveCount(0);

  // Owners cannot change their own role from this page.
  await expect(page.getByRole("combobox", { name: "Role for Pat Boss" })).toHaveCount(0);

  await page.getByRole("button", { name: "Deactivate" }).click();
  await expect(page.getByText("Sign them out and block access?")).toBeVisible();
  expect(requests.length).toBe(1);
  await page.getByRole("button", { name: "Deactivate" }).click();
  await expect.poll(() => requests.length).toBe(2);
  expect(requests[1]).toEqual(["patch", { active: false }]);
  await expect(page.getByText("Deactivated", { exact: true })).toBeVisible();
});

test("non-owners do not see team management", async ({ page }) => {
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return json(route, profile("manager"));
    return json(route, { detail: "Not found" }, 404);
  });
  await page.goto("/team");
  await expect(page.getByText("Only company owners can manage the team.", { exact: false })).toBeVisible();
  await expect(page.getByRole("link", { name: "Team", exact: true })).toHaveCount(0);
});
