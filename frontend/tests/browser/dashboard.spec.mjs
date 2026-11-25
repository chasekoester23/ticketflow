import { expect, test } from "@playwright/test";
import { chooseProject, json, mockWorkspace } from "./f_helpers.mjs";

const byPath = (handlers) => (_request, path) => handlers[path];

const DASHBOARD = {
  tickets: 412, needs_review: 37, flagged: 4, failed: 2, verified: 360, total_tons: "8412.250", pending_tons: "801.000",
  total_cost: "155626.63", unpriced_tickets: 6, pending_cost_tickets: 52,
  by_supplier: [{ name: "Example Aggregates", tickets: 412, tons: "8412.250", cost: "155626.63" }], by_material: [],
  recent_batches: [{ id: 42, status: "ready_for_review", created_at: new Date(Date.now() - 20 * 60000).toISOString(), files: 3, project: "Fictional Road Job" }],
  projects: [
    { id: 1, code: "24-115", name: "Fictional Road Job", tickets: 300, open: 31, verified_tons: "6200.500", last_upload: new Date(Date.now() - 20 * 60000).toISOString() },
    { id: 2, code: "24-130", name: "Fictional Bridge Deck", tickets: 112, open: 6, verified_tons: "2211.750", last_upload: null },
  ],
  activity: [
    { at: new Date(Date.now() - 3 * 60000).toISOString(), action: "approved", ticket_id: 901, ticket_number: "48213", batch_id: 42, by: "Dana Reyes" },
    { at: new Date(Date.now() - 9 * 60000).toISOString(), action: "flagged", ticket_id: 902, ticket_number: "48214", batch_id: 42, by: "Cal Clerk" },
  ],
};

test("the dashboard leads with what needs doing, by real counts, and links to each", async ({ page }) => {
  await mockWorkspace(page, { extra: byPath({
    "/api/dashboard": (route) => json(route, DASHBOARD),
    "/api/onboarding": (route) => json(route, { complete: false, steps: [{ key: "approve", title: "Approve", detail: "", href: "/upload", done: true }] }),
    "/api/invoice-comparisons/overview": (route) => json(route, { lines: 9, by_status: { discrepancy: 3, matches: 6 }, billed: "0", verified: "0", lines_unresolved: 0, by_supplier: [], exceptions: [] }),
  }) });
  await page.goto("/");
  const attention = page.getByRole("region", { name: "Needs attention" });
  await expect(attention).toContainText("46 open");
  for (const [label, count, href] of [["Awaiting verification", "31", "/records?status=needs_review"], ["Flagged for follow-up", "4", "/records?status=flagged"],
    ["Couldn't be read", "2", "/records?status=failed"], ["Approved without a rate", "6", "/pricing"], ["Invoice lines with differences", "3", "/invoices"]]) {
    const row = attention.getByRole("link", { name: new RegExp(label) });
    await expect(row).toContainText(count);
    await expect(row).toHaveAttribute("href", href);
  }
  const projects = page.getByRole("table", { name: "Active projects" });
  await expect(projects.getByRole("row").nth(1)).toContainText("24-115");
  await expect(projects.getByRole("row").nth(1)).toContainText("6,200.5");
  await expect(projects.getByRole("row").nth(2)).toContainText("No uploads");
  await expect(projects.getByRole("link", { name: "24-115" })).toHaveAttribute("href", "/records?project_id=1");
  const activity = page.getByRole("region", { name: "Recent activity" });
  await expect(activity).toBeHidden(); // tucked away until asked for
  await page.getByText("Recent uploads and activity").click();
  await expect(activity.getByRole("listitem").first()).toContainText("Approved ticket 48213 · Dana Reyes");
  await expect(activity.getByRole("link", { name: "ticket 48213" })).toHaveAttribute("href", "/upload?batch=42&ticket=901");
  await expect(page.getByRole("region", { name: "Setup checklist" })).toHaveCount(0);
});

test("an older server without the new sections still shows a working dashboard", async ({ page }) => {
  const { projects, activity, failed, ...older } = DASHBOARD;
  await mockWorkspace(page, { extra: byPath({ "/api/dashboard": (route) => json(route, older) }) });
  await page.goto("/");
  await expect(page.getByRole("region", { name: "Needs attention" })).toContainText("Awaiting verification");
  await expect(page.getByRole("region", { name: "Active projects" })).toContainText("No active projects");
  await page.getByText("Recent uploads and activity").click();
  await expect(page.getByRole("region", { name: "Recent activity" })).toContainText("will appear here");
});

test("the totals lead with quantities and costs; invoice exceptions are one row that opens the invoice workflow", async ({ page }) => {
  const requested = [];
  const summary = { open: 7, by_category: {}, by_severity: { high: 2, medium: 3, low: 2 }, potential_overcharge: "1240.50",
    billed_below_approved: "0", disputed_amount: "310.00", recovered: "95.25", money_unknown: 0, unassigned: 4 };
  const hauling = { approved_cost: "12500.00", approved_hours: "118.500", approved_with_material: "168126.63", awaiting_approval: 3, disputed: 1 };
  await mockWorkspace(page, { extra: (request, path) => {
    if (path === "/api/exceptions") { requested.push(new URL(request.url()).search); return (route) => json(route, { summary, items: [] }); }
    return {
      "/api/dashboard": (route) => json(route, { ...DASHBOARD, hauling }),
      "/api/onboarding": (route) => json(route, { complete: true, steps: [] }),
      "/api/invoice-comparisons/overview": (route) => json(route, { lines: 9, by_status: {}, billed: "48210.00", verified: "0", lines_unresolved: 0, by_supplier: [], exceptions: [] }),
    }[path];
  } });
  await page.goto("/");
  const totals = page.getByRole("region", { name: "Totals" });
  await expect(totals).toContainText("8,412.25");
  await expect(totals).toContainText("$155,626.63");
  await expect(totals).toContainText("$12,500.00");
  await expect(totals).toContainText("$168,126.63");
  await expect(totals).toContainText("37 tickets · 3 truck shifts");
  await expect(page.getByRole("region", { name: "Invoice check" })).toHaveCount(0);
  const attention = page.getByRole("region", { name: "Needs attention" });
  await expect(attention.getByRole("link", { name: /Truck shifts to approve/ })).toHaveAttribute("href", "/trucking?status=needs_review");
  const row = attention.getByRole("link", { name: /Invoice exceptions/ });
  await expect(row).toContainText("7");
  await expect(row).toHaveAttribute("href", "/invoices/exceptions");
  await chooseProject(page, "24-115");
  await expect(page.getByTestId("overview-scope")).toHaveText("24-115 · Fictional Road Job");
  await expect.poll(() => requested.at(-1)).toBe("?project_id=1");
});
