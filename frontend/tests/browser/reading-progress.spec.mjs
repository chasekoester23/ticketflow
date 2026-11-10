import { expect, test } from "@playwright/test";
import { complete, json, mockReview } from "./f_helpers.mjs";

const reading = (progress, status = "processing") => ({
  id: 5, project_id: 1, layout: "grid_2x2", intake_mode: "extract", status, created_at: "2026-11-10T12:00:00Z",
  expected_ticket_count: null, files: [{ id: 1, original_name: "scan.pdf", status, error: null }], progress,
});

test("while a batch is read, ready tickets can be reviewed and the page says how many are left", async ({ page }) => {
  const tickets = [complete(101), complete(102)];
  let batch = reading({ expected: 4, read: 2, failed: 0, reading_again: 0, files_waiting: 0 });
  await mockReview(page, tickets, { intercept: (_request, path) => path === "/api/batches/5" ? (route) => json(route, batch) : null });
  await page.goto("/upload?batch=5&ticket=102");
  const progress = page.getByTestId("reading-progress");
  await expect(progress).toContainText("Reading tickets: 2 of 4 ready");
  await expect(progress).toContainText("Ready tickets can be reviewed now.");
  await expect(page.getByLabel("Ticket number")).toHaveValue("10102");
  await page.getByLabel("Tonnage (tons)").fill("21.250");

  // Two more arrive, one of them earlier in the scan than the ticket being worked on.
  tickets.unshift(complete(100, { position: -1 }));
  tickets.push(complete(103));
  batch = reading({ expected: 4, read: 3, failed: 1, reading_again: 0, files_waiting: 0 });
  await expect(progress).toContainText("3 of 4 ready · 1 couldn't be read");
  await expect(page.getByLabel("Ticket number")).toHaveValue("10102");
  await expect(page.getByLabel("Tonnage (tons)")).toHaveValue("21.250");
  await expect(page.getByTestId("position")).toContainText("Ticket 3 of 4");

  batch = reading(null, "ready_for_review");
  await expect(progress).toHaveCount(0);
});
