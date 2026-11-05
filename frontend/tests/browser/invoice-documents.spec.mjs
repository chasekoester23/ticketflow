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
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==", "base64");
const LINE = {
  id: 5, page: 1, source_text: "No. 53 Crushed Stone 46.600 TON 30.0000 1,398.00", state: "pending_review", project_id: 1,
  invoice_number: "EA-2026-1107", supplier_name: "Example Aggregates", material_name: "No. 53 Crushed Stone",
  billed_quantity: "46.600", unit: "ton", billed_unit_price: "30.0000", billed_subtotal: "1398.00",
  date_from: "2026-10-01", date_to: "2026-10-31", ticket_list: "10482\t24.50", comparison_id: null, blockers: [], warnings: [],
};
const DOC = {
  id: 9, project_id: 1, original_name: "fictional-invoice.pdf", sha256: "a".repeat(64), page_count: 2, status: "needs_review",
  extractor: "text_layer", message: null, unread_rows: ["Freight 120.00"], created_at: "2026-11-05T10:00:00Z", lines: [LINE],
};

async function mock(page, role, doc, calls = []) {
  let current = doc;
  await page.route("**/api/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return json(route, profile(role));
    if (path === "/api/projects") return json(route, [{ id: 1, code: "24-115", name: "Fictional Road Job", job_location: null, description: null, status: "active" }]);
    if (/\/image$/.test(path)) return route.fulfill({ status: 200, contentType: "image/png", headers: cors, body: PNG });
    if (request.method() !== "GET") {
      calls.push({ path, method: request.method(), body: request.postData() });
      if (path.endsWith("/confirm")) current = { ...current, lines: [{ ...current.lines[0], state: "confirmed", comparison_id: 31 }] };
      if (request.method() === "PATCH") current = { ...current, lines: [{ ...current.lines[0], ...request.postDataJSON() }] };
      return json(route, current);
    }
    if (path === "/api/invoice-documents") return json(route, [current]);
    if (path === "/api/invoice-documents/9") return json(route, current);
    return json(route, { detail: "Not found" }, 404);
  });
}

test("the original sits beside the draft, and confirming is an explicit step after saving", async ({ page }) => {
  const calls = [];
  await mock(page, "manager", DOC, calls);
  await page.goto("/invoices/documents?id=9");
  const review = page.getByRole("region", { name: "Invoice review" });
  await expect(review.getByRole("img", { name: "Page 1 of fictional-invoice.pdf" })).toHaveAttribute("src", /\/api\/invoice-documents\/9\/pages\/1\/image$/);
  await expect(review.getByText("Read from the PDF: No. 53 Crushed Stone", { exact: false })).toBeVisible();
  await expect(review.getByText("Freight 120.00")).toBeVisible();
  await review.getByRole("button", { name: "Next" }).click();
  await expect(review.getByRole("img", { name: "Page 2 of fictional-invoice.pdf" })).toBeVisible();

  await review.getByLabel("Rate").fill("31.0000");
  await expect(review.getByRole("button", { name: "Confirm line" })).toBeDisabled(); // unsaved edits can't be confirmed
  await review.getByRole("button", { name: "Save changes" }).click();
  await expect.poll(() => calls.length).toBe(1);
  expect(JSON.parse(calls[0].body)).toEqual({ billed_unit_price: "31.0000" });
  await expect(review.getByRole("button", { name: "Confirm line" })).toBeEnabled();
  await review.getByRole("button", { name: "Confirm line" }).click();
  await expect.poll(() => calls.map((c) => c.path)).toContain("/api/invoice-documents/9/lines/5/confirm");
  await expect(review.getByText("Saved as comparison #31")).toBeVisible();
  await expect(review.getByRole("button", { name: "Confirm line" })).toHaveCount(0);
});

test("a line with blockers can't be confirmed, and an unsupported document says why", async ({ page }) => {
  await mock(page, "owner", { ...DOC, status: "unsupported", message: "This PDF has no readable text, so it looks scanned.",
    unread_rows: [], lines: [{ ...LINE, source_text: null, unit: "cy", blockers: ["billed in 'cy': comparison works in tons only, and units are never converted"] }] });
  await page.goto("/invoices/documents?id=9");
  await expect(page.getByRole("note").filter({ hasText: "looks scanned" })).toBeVisible();
  await expect(page.getByText("units are never converted")).toBeVisible();
  await expect(page.getByRole("button", { name: "Confirm line" })).toBeDisabled();
  await expect(page.getByRole("button", { name: /Add a line by hand/ })).toBeVisible();
});

test("a viewer can read the original and drafts but change nothing", async ({ page }) => {
  await mock(page, "viewer", DOC);
  await page.goto("/invoices/documents?id=9");
  await expect(page.getByRole("region", { name: "Invoice review" }).getByLabel("Rate")).toHaveAttribute("readonly", "");
  for (const name of ["Confirm line", "Save changes", "Reject", "Upload invoice", "Wrong file — void it"]) {
    await expect(page.getByRole("button", { name })).toHaveCount(0);
  }
});
