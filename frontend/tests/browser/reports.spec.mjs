import { expect, test } from "@playwright/test";
import { profile } from "./accounts.mjs";

const cors = {
  "access-control-allow-origin": "http://127.0.0.1:3000",
  "access-control-allow-credentials": "true",
  "access-control-allow-headers": "content-type, x-ticketflow-request",
  "access-control-allow-methods": "GET, POST, OPTIONS",
};
const json = (route, value, status = 200) => route.fulfill({
  status, contentType: "application/json", headers: cors, body: JSON.stringify(value),
});
const REPORT = {
  project_id: null, unresolved_items: 3,
  intake: { batches: 1, files: 1, failed_files: 0, files_without_scan_check: 0, pages: 1, slots: 4, faint_unclassified: 1,
    empty: 1, confirmed_blank: 0, batches_with_paper_count: 1, paper_count_mismatches: 1 },
  review: { records: 2, needs_review: 1, flagged: 0, failed: 0, verified: 1, skipped: 0, with_unreadable_fields: 0,
    verified_unpriced: 0, verified_tons: "24.500" },
  reconciliation: { invoice_lines: 1, itemized_lines: 0, totals_only_lines: 1, lines_by_status: { discrepancy: 1 },
    lines_needing_attention: 1, verified_tickets_linked: 0, verified_tickets_not_linked: 1, tons_linked: "0", tons_not_linked: "24.500" },
  batches_needing_attention: [{ batch_id: 7, reasons: ["1 faint slot(s) not classified"] }],
  review_timing: { enabled: false, manual: { samples: 0, median_seconds: null, with_corrections: 0 }, extract: { samples: 0, median_seconds: null, with_corrections: 0 } },
};

test("the report states what needs a person, links to it, and makes no completeness claim", async ({ page }) => {
  const asked = [];
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (url.pathname === "/api/auth/me") return json(route, profile("viewer"));
    if (url.pathname === "/api/projects") return json(route, [{ id: 1, code: "24-115", name: "Fictional Road Job", job_location: null, description: null, status: "active" }]);
    if (url.pathname === "/api/reports/pilot") { asked.push(url.search); return json(route, REPORT); }
    return json(route, { detail: "Not found" }, 404);
  });
  await page.goto("/reports");
  await expect(page.getByTestId("unresolved")).toContainText("3 items need a person");
  await expect(page.getByRole("link", { name: "Open scan check" })).toHaveAttribute("href", "/upload?batch=7");
  await expect(page.getByRole("region", { name: "Review" }).getByRole("row").filter({ hasText: "Needs review" }).getByRole("link", { name: "Show" }))
    .toHaveAttribute("href", /status=needs_review/);
  await expect(page.getByRole("region", { name: "Invoice reconciliation" })).toContainText("Totals only, tickets not checked");
  await expect(page.getByRole("region", { name: "Review time (opt-in)" })).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText("%");

  await page.getByLabel("Project").selectOption("1");
  await expect.poll(() => asked).toContain("?project_id=1");
  await expect(page.getByRole("link", { name: "Download CSV" })).toHaveAttribute("href", /pilot\.csv\?project_id=1$/);
});
