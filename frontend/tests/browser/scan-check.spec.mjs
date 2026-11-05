import { expect, test } from "@playwright/test";
import { profile } from "./accounts.mjs";

const cors = {
  "access-control-allow-origin": "http://127.0.0.1:3000",
  "access-control-allow-credentials": "true",
  "access-control-allow-headers": "content-type, x-ticketflow-request",
  "access-control-allow-methods": "GET, POST, PUT, PATCH, OPTIONS",
};
const json = (route, value, status = 200) => route.fulfill({
  status, contentType: "application/json", headers: cors, body: JSON.stringify(value),
});
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==", "base64");

const slot = (id, position, state, extra = {}) => ({
  id, position, state, detection: state === "ticket" ? "content" : state === "suspected_blank" ? "faint" : "empty",
  decision: null, rotation: 0, crop_box: [0, 0, 10, 10], crop_sha256: "a".repeat(64),
  ticket_id: null, ticket_number: null, ticket_status: null, ...extra,
});
function manifest(overrides = {}, slotOverride) {
  return {
    batch_id: 7, layout: "grid_2x2", status: "needs_attention", expected_ticket_count: 4,
    counts: { files: 1, failed_files: 0, legacy_files: 0, pages: 1, slots: 4, records: 2, suspected_blank: 1,
      empty: 1, confirmed_blank: 0, needs_review: 2, flagged: 0, verified: 0, failed_tickets: 0, skipped: 0 },
    blockers: ["1 slot(s) look faint. Mark each as a real ticket or confirm it is blank.",
      "You expected 4 ticket(s), but 2 are on record."],
    files: [{ id: 3, original_name: "fictional-scan.pdf", sha256: "b".repeat(64), status: "ready_for_review", error: null,
      legacy: false, pages: [{ page: 1, slots: slotOverride ?? [
        slot(1, 0, "ticket", { ticket_id: 11, ticket_number: "10482", ticket_status: "needs_review" }),
        slot(2, 1, "suspected_blank"), slot(3, 2, "empty"),
        slot(4, 3, "ticket", { ticket_id: 12, ticket_number: "10483", ticket_status: "verified" }),
      ] }] }],
    ...overrides,
  };
}
const ticket = (id, number, position) => ({
  id, batch_id: 7, file_id: 3, source_page: 1, position, ticket_number: number, delivery_date: "2026-10-15",
  material_type: "Sand", tonnage: "20.000", supplier: "Example Aggregates", job_location: "North Lot",
  flags: [], status: "needs_review", tonnage_confirmed: false, error: null, duplicates: [], pricing_notes: [],
});

async function mock(page, role, posts = [], current = { value: manifest() }) {
  await page.route("**/api/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return json(route, profile(role));
    if (path === "/api/status") return json(route, { extraction_configured: true });
    if (path === "/api/projects") return json(route, []);
    if (path === "/api/batches/7") return json(route, { id: 7, project_id: 1, layout: "grid_2x2", intake_mode: "manual",
      status: "ready_for_review", created_at: "2026-11-05T10:00:00Z", expected_ticket_count: 4,
      files: [{ id: 3, original_name: "fictional-scan.pdf", status: "ready_for_review", error: null }] });
    if (path === "/api/batches/7/tickets") return json(route, [ticket(11, "10482", 0), ticket(12, "10483", 3)]);
    if (path === "/api/batches/7/manifest") return json(route, current.value);
    if (/\/image$/.test(path)) return route.fulfill({ status: 200, contentType: "image/png", headers: cors, body: PNG });
    if (/\/audit$/.test(path)) return json(route, []);
    if (request.method() !== "GET") {
      posts.push({ path, body: request.postData() });
      current.value = manifest({ counts: { ...manifest().counts, suspected_blank: 0, records: 3 } });
      return json(route, current.value);
    }
    return json(route, { detail: "Not found" }, 404);
  });
}

test("the scan check shows every slot, blockers, and lets a clerk mark a faint ticket", async ({ page }) => {
  const posts = [];
  await mock(page, "clerk", posts);
  await page.goto("/upload?batch=7");
  const panel = page.getByRole("region", { name: "Scan check" });
  await expect(panel.getByText("1 page · 4 slots · 2 tickets on record", { exact: false })).toBeVisible();
  await expect(panel.getByText("Needs attention")).toBeVisible();
  await expect(panel.getByText("You expected 4 ticket(s), but 2 are on record.")).toBeVisible();
  await expect(panel.getByRole("img", { name: "Original page 1 of fictional-scan.pdf" })).toHaveAttribute("src", /\/api\/files\/3\/pages\/1\/image$/);
  await expect(panel.getByRole("article")).toHaveCount(4);

  const faint = panel.getByRole("article", { name: "Page 1 slot 2" });
  await expect(faint).toContainText("Faint — is this a ticket?");
  // Clerks can add a record but cannot rule a slot out of the count.
  await expect(faint.getByRole("button", { name: "Confirm blank" })).toHaveCount(0);
  await faint.getByRole("button", { name: "This is a ticket" }).click();
  await expect.poll(() => posts.map((p) => p.path)).toEqual(["/api/scan-slots/2/mark-ticket"]);
  // Resolving the last faint slot must not collapse the section.
  await expect(panel.getByText("0 faint", { exact: false })).toBeVisible();
  await expect(panel.getByRole("article", { name: "Page 1 slot 1" }).getByRole("button", { name: "Open ticket" })).toBeVisible();

  // An approved ticket's image can't be rotated from here.
  const approved = panel.getByRole("article", { name: "Page 1 slot 4" });
  await expect(approved.getByRole("button", { name: "Rotate 90°" })).toHaveCount(0);
});

test("a manager can confirm blank and correct the paper count", async ({ page }) => {
  const posts = [];
  await mock(page, "manager", posts);
  await page.goto("/upload?batch=7");
  const panel = page.getByRole("region", { name: "Scan check" });
  await panel.getByRole("article", { name: "Page 1 slot 2" }).getByRole("button", { name: "Confirm blank" }).click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0].path).toBe("/api/scan-slots/2/confirm-blank");
  await panel.getByLabel("Paper tickets you scanned").fill("3");
  await panel.getByRole("button", { name: "Save count" }).click();
  await expect.poll(() => posts.length).toBe(2);
  expect(posts[1].path).toBe("/api/batches/7/expected-count");
  expect(JSON.parse(posts[1].body)).toEqual({ expected_ticket_count: 3 });
});

test("a viewer sees the evidence with no actions, and a matching count is plainly labelled", async ({ page }) => {
  await mock(page, "viewer", [], { value: manifest({ status: "count_matches", blockers: [], counts: { ...manifest().counts, suspected_blank: 0 } }, [slot(1, 0, "ticket", { ticket_id: 11, ticket_number: "10482", ticket_status: "verified" })]) });
  await page.goto("/upload?batch=7");
  const panel = page.getByRole("region", { name: "Scan check" });
  await expect(panel.getByText("Matches your paper count")).toBeVisible();
  await panel.getByText("Pages and slots").click();
  await expect(panel.getByRole("button", { name: "This is a ticket" })).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "Rotate 90°" })).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "Save count" })).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "Open ticket" })).toBeVisible();
});

test("older batches offer to build the scan check", async ({ page }) => {
  const posts = [];
  await mock(page, "owner", posts, { value: manifest({
    counts: { ...manifest().counts, legacy_files: 1, slots: 0, pages: 0 },
    blockers: ["1 older file(s) have no slot record yet, so blank-looking slots cannot be checked. Build the scan check to review them."],
    files: [{ id: 3, original_name: "old.pdf", sha256: "c".repeat(64), status: "ready_for_review", error: null, legacy: true, pages: [] }],
  }) });
  await page.goto("/upload?batch=7");
  await page.getByRole("button", { name: "Build scan check" }).click();
  await expect.poll(() => posts.map((p) => p.path)).toEqual(["/api/batches/7/manifest/rebuild"]);
});
