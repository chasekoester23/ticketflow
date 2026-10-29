import { expect, test } from "@playwright/test";

const cors = {
  "access-control-allow-origin": "http://127.0.0.1:3000",
  "access-control-allow-credentials": "true",
  "access-control-allow-headers": "content-type, x-ticketflow-request",
  "access-control-allow-methods": "GET, POST, PATCH, OPTIONS",
};
const json = (route, data, status = 200) => route.fulfill({
  contentType: "application/json", status, headers: cors, body: JSON.stringify(data),
});

test("manual scan upload is visibly non-AI and sends deliberate intake mode", async ({ page }) => {
  const received = [];
  const batch = {
    id: 83, project_id: 1, layout: "grid_2x2", intake_mode: "manual",
    status: "uploaded", created_at: "2026-10-29T11:00:00Z",
    files: [{ id: 10, original_name: "demo.pdf", status: "uploaded", error: null }],
  };
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return json(route, { id: 1, username: "test-owner" });
    if (path === "/api/status") return json(route, { extraction_configured: false, model: "unused" });
    if (path === "/api/projects") return json(route, [{
      id: 1, code: "DEMO-001", name: "Synthetic Demonstration", status: "active",
      job_location: null, description: null,
    }]);
    if (path === "/api/batches" && request.method() === "POST") {
      received.push(request.postData() || "");
      return json(route, batch);
    }
    if (path === "/api/batches/83") return json(route, batch);
    if (path === "/api/batches/83/tickets") return json(route, []);
    return json(route, { detail: "Unmocked" }, 404);
  });
  await page.goto("/upload");
  await expect(page.getByText("Manual entry works without an API key.")).toBeVisible();
  await expect(page.getByRole("radio", { name: /Manual entry/ })).toBeChecked();
  await page.getByRole("combobox", { name: "Project" }).selectOption("1");
  await page.locator('input[type="file"]').setInputFiles({
    name: "demo.pdf", mimeType: "application/pdf",
    buffer: Buffer.from("%PDF-1.4\nMock synthetic PDF\n"),
  });
  await page.getByRole("button", { name: "Queue for manual entry" }).click();
  await expect(page.getByText("Batch #83", { exact: false })).toBeVisible();
  await expect(page.getByText("prepare ticket images for manual entry", { exact: false })).toBeVisible();
  expect(received).toHaveLength(1);
  expect(received[0]).toContain('name="intake_mode"');
  expect(received[0]).toContain("manual");
  expect(received[0]).not.toContain("ANTHROPIC_API_KEY");
});
