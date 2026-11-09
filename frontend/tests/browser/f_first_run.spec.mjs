import { expect, test } from "@playwright/test";
import { profile } from "./accounts.mjs";
import { cors, json, mockWorkspace } from "./f_helpers.mjs";

async function mockSetup(page, { needed = true, answer } = {}) {
  const log = { setups: [], headers: [] };
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/setup/status") return typeof needed === "number" ? json(route, { detail: "Not Found" }, needed) : json(route, { needed: needed && !log.setups.length });
    if (path === "/api/setup" && request.method() === "POST") {
      log.setups.push(request.postDataJSON());
      log.headers.push(request.headers()["x-ticketflow-request"]);
      return answer ? answer(route) : json(route, profile("owner", { display_name: "Fictional Owner", organization_name: "Fictional Paving Co" }));
    }
    if (path === "/api/auth/me") return log.setups.length && !answer ? json(route, profile("owner", { organization_name: "Fictional Paving Co" })) : json(route, { detail: "Sign in" }, 401);
    if (path === "/api/projects") return json(route, []);
    if (path === "/api/dashboard") return json(route, { tickets: 0, needs_review: 0, flagged: 0, verified: 0, total_tons: "0", pending_tons: "0", total_cost: "0", unpriced_tickets: 0, pending_cost_tickets: 0, by_supplier: [], by_material: [], recent_batches: [] });
    if (path === "/api/onboarding") return json(route, { complete: false, steps: [{ key: "project", title: "Create a project", detail: "Each upload belongs to one job.", href: "/projects", done: false }] });
    return json(route, { detail: "Not Found" }, 404);
  });
  return log;
}

async function fillSetup(page, password = "a long fictional passphrase") {
  const form = page.getByRole("form", { name: "Set up TicketFlow" });
  await form.getByLabel("Company name").fill("Fictional Paving Co");
  await form.getByLabel("Your name").fill("Fictional Owner");
  await form.getByLabel("Username").fill("owner");
  await form.getByLabel("Password", { exact: true }).fill(password);
  await form.getByLabel("Password again").fill(password);
  return form;
}

test("a server with no accounts leads to setup, which creates the owner and signs them in", async ({ page }) => {
  const log = await mockSetup(page);
  await page.goto("/login");
  await expect(page.getByRole("heading", { name: "Set up TicketFlow" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Sign in to TicketFlow" })).toHaveCount(0);
  const form = await fillSetup(page);
  await form.getByRole("button", { name: "Create company and sign in" }).click();
  await expect(page).toHaveURL("/");
  expect(log.setups).toEqual([{ company_name: "Fictional Paving Co", display_name: "Fictional Owner", username: "owner", password: "a long fictional passphrase" }]);
  expect(log.headers).toEqual(["1"]);
});

test("setup checks the password before sending anything", async ({ page }) => {
  const log = await mockSetup(page);
  await page.goto("/login");
  const form = await fillSetup(page, "short");
  await expect(form.getByText("Use 12 to 1024 characters")).toBeVisible();
  await expect(form.getByLabel("Password", { exact: true })).toHaveAttribute("aria-invalid", "true");
  await expect(form.getByRole("button", { name: "Create company and sign in" })).toBeDisabled();
  await form.getByLabel("Password", { exact: true }).fill("a long fictional passphrase");
  await form.getByLabel("Password again").fill("a long fictional passphrasf");
  await expect(form.getByText("The two passwords don't match.")).toBeVisible();
  await expect(form.getByRole("button", { name: "Create company and sign in" })).toBeDisabled();
  await form.getByLabel("Username").fill("a long fictional passphrase");
  await form.getByLabel("Password again").fill("a long fictional passphrase");
  await expect(form.getByText("can't be your username")).toBeVisible();
  await expect(form.getByRole("button", { name: "Create company and sign in" })).toBeDisabled();
  expect(log.setups).toEqual([]);
});

test("if someone set it up meanwhile, setup says so and offers sign-in", async ({ page }) => {
  await mockSetup(page, { answer: (route) => json(route, { detail: { code: "already_set_up", message: "TicketFlow was just set up by someone else. Sign in, or ask them to add you." } }, 409) });
  await page.goto("/login");
  const form = await fillSetup(page);
  await form.getByRole("button", { name: "Create company and sign in" }).click();
  await expect(page.getByRole("heading", { name: "Sign in to TicketFlow" })).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "just set up by someone else" })).toBeVisible();
});

test("a refusal from another computer is shown as the server words it", async ({ page }) => {
  await mockSetup(page, { answer: (route) => json(route, { detail: { code: "setup_local_only", message: "First-time setup only works from the computer TicketFlow runs on. Open it there, or set ALLOW_REMOTE_SETUP=true for this one step." } }, 403) });
  await page.goto("/login");
  const form = await fillSetup(page);
  await form.getByRole("button", { name: "Create company and sign in" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "First-time setup" })).toContainText("only works from the computer TicketFlow runs on");
});

for (const [name, needed] of [["already has accounts", false], ["doesn't offer setup yet", 404]]) {
  test(`a server that ${name} shows the usual sign-in`, async ({ page }) => {
    await mockSetup(page, { needed });
    await page.goto("/login");
    await expect(page.getByRole("heading", { name: "Sign in to TicketFlow" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Set up TicketFlow" })).toHaveCount(0);
  });
}

const EMPTY_STATES = [
  ["/projects", "projects-empty", "Add your first job above", "A manager or owner adds each job here"],
  ["/pricing", "suppliers-empty", "Add each company that delivers", "A manager or owner sets up suppliers"],
  ["/pricing", "rates-empty", "Add a supplier and a material above first", "Approved tickets show no cost until a manager or owner sets the rate"],
  ["/records", "records-empty", "Upload a batch of scanned tickets", "once someone on your team uploads and enters them"],
  ["/invoices", "invoices-empty", "create a project", "A manager or owner records each supplier invoice line"],
];
for (const [path, id, forManager, forViewer] of EMPTY_STATES) {
  for (const [role, text] of [["owner", forManager], ["viewer", forViewer]]) {
    test(`an empty ${path} tells ${role === "owner" ? "an owner" : "a viewer"} what happens next (${id})`, async ({ page }) => {
      await mockWorkspace(page, { role, empty: true });
      await page.goto(path);
      await expect(page.getByTestId(id)).toContainText(text);
    });
  }
}

test("empty invoices, once a project exists, say how to start", async ({ page }) => {
  await mockWorkspace(page, { empty: true, extra: (request, path) => path === "/api/projects" ? (route) => json(route, [{ id: 1, code: "24-115", name: "Fictional Road Job", job_location: null, description: null, status: "active" }]) : null });
  await page.goto("/invoices");
  await expect(page.getByTestId("invoices-empty")).toContainText("upload the invoice PDF");
  await expect(page.getByTestId("invoices-empty")).toContainText("Include the invoice's ticket numbers");
});

test("a saved view can name its columns to match a team's spreadsheet", async ({ page }) => {
  const posted = [];
  await mockWorkspace(page, { extra: (request, path, method) => {
    if (path !== "/api/saved-views" || method !== "POST") return null;
    posted.push(request.postDataJSON());
    return (route) => json(route, { id: 2, created_by: "Fictional Owner", created_at: "2026-11-07T00:00:00Z", ...request.postDataJSON() }, 201);
  } });
  await page.goto("/records");
  await page.getByRole("button", { name: "Save this view" }).click();
  const dialog = page.getByRole("dialog", { name: "Save this view" });
  await dialog.getByLabel("Name", { exact: true }).fill("Weekly to accounts");
  await dialog.getByText("Name these columns (optional)").click();
  await dialog.getByLabel("Header for Ticket number").fill("Ticket #");
  await dialog.getByLabel("Header for Tons").fill("  Net Tons  ");
  await dialog.getByRole("button", { name: "Save view" }).click();
  await expect.poll(() => posted.length).toBe(1);
  expect(posted[0].labels).toEqual({ ticket_number: "Ticket #", tons: "Net Tons" });
  expect(posted[0].name).toBe("Weekly to accounts");
});

test("naming no columns sends no labels, so an older server still accepts the view", async ({ page }) => {
  const posted = [];
  await mockWorkspace(page, { extra: (request, path, method) => {
    if (path !== "/api/saved-views" || method !== "POST") return null;
    posted.push(request.postDataJSON());
    return (route) => json(route, { id: 2, created_by: "x", created_at: "2026-11-07T00:00:00Z", ...request.postDataJSON() }, 201);
  } });
  await page.goto("/records");
  await page.getByRole("button", { name: "Save this view" }).click();
  const dialog = page.getByRole("dialog", { name: "Save this view" });
  await dialog.getByLabel("Name", { exact: true }).fill("Plain");
  await dialog.getByRole("button", { name: "Save view" }).click();
  await expect.poll(() => posted.length).toBe(1);
  expect("labels" in posted[0]).toBe(false);
});

test("with a saved view open, exports carry its id, so its column names apply", async ({ page }) => {
  await mockWorkspace(page);
  await page.goto("/records");
  await page.getByLabel("Saved view").selectOption({ label: "October stone" });
  const csv = page.getByRole("link", { name: "Export CSV" });
  await expect(csv).toHaveAttribute("href", /export\.csv\?material=stone&view_id=1&columns=ticket_number,tons$/);
  await page.getByRole("button", { name: "Choose columns" }).click();
  const dialog = page.getByRole("dialog", { name: "Export columns" });
  await expect(dialog).toContainText("as “Net Tons”");
  await expect(dialog).toContainText("Exports use the column names saved with “October stone”.");
  await dialog.getByLabel("Supplier").check();
  await dialog.getByRole("button", { name: "Done" }).click();
  // The server uses the columns chosen here and the view's names for them.
  await expect(csv).toHaveAttribute("href", /view_id=1&columns=ticket_number,tons,supplier$/);
  await page.getByLabel("Saved view").selectOption("");
  await expect(csv).not.toHaveAttribute("href", /view_id/);
});

test("a viewer isn't offered actions their role can't take", async ({ page }) => {
  await mockWorkspace(page, { role: "viewer" });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Delivery operations" })).toBeVisible();
  await expect(page.getByRole("link", { name: /New ticket upload/ })).toHaveCount(0);
  await page.goto("/records");
  await expect(page.getByTestId("records-summary")).toContainText("3 records");
  await expect(page.getByRole("link", { name: /New ticket upload/ })).toHaveCount(0);
  await page.getByLabel("Saved view").selectOption({ label: "October stone" });
  await expect(page.getByRole("button", { name: "Remove view" })).toHaveCount(0);
});

test("a clerk sees a ticket's rate, without a field they can't change", async ({ page }) => {
  await mockWorkspace(page, { role: "clerk" });
  await page.goto("/upload?batch=5&ticket=1001");
  await expect(page.getByRole("button", { name: "Save & next" })).toBeVisible();
  await expect(page.getByLabel(/Override rate/)).toHaveCount(0);
  await expect(page.getByText("Only a manager or owner can change a ticket’s rate.")).toBeVisible();
});
