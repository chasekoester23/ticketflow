import { expect, test } from "@playwright/test";

function fixture(id, number) {
  return {
    id, batch_id: 5, file_id: 1, source_page: 1, position: id - 101,
    ticket_number: number, delivery_date: "2026-10-15",
    material_type: "No. 53 Stone", tonnage: "24.500", supplier: "Example Aggregates",
    job_location: "Project A", flags: [], status: "needs_review",
    tonnage_confirmed: false, error: null, unit_price_override: null,
    price_snapshot_state: "pending", unit_price: "30.0000",
    price_source: "rate", cost: null, duplicates: [], pricing_notes: [],
  };
}

async function launch(page, deepLink = 101) {
  const tickets = [fixture(101, "10482"), fixture(102, "10483")];
  const log = { verified: [], flagged: [], patched: [] };
  const cors = {
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET, POST, PATCH, OPTIONS",
    "access-control-allow-headers": "content-type",
  };
  const json = (route, value, status = 200) => route.fulfill({
    status, contentType: "application/json", headers: cors, body: JSON.stringify(value),
  });
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/status") return json(route, { extraction_configured: true, model: "test-only" });
    if (path === "/api/projects") return json(route, [{
      id: 1, code: "P-100", name: "Project A", job_location: "Site", description: null, status: "active",
    }]);
    if (path === "/api/batches/5") return json(route, {
      id: 5, project_id: 1, layout: "grid_2x2", status: "ready_for_review",
      created_at: "2026-10-15T11:00:00Z",
      files: [{ id: 1, original_name: "test.pdf", status: "ready_for_review", error: null }],
    });
    if (path === "/api/batches/5/tickets") return json(route, tickets.map((ticket) => ({ ...ticket })));
    if (/^\/api\/tickets\/\d+\/image$/.test(path)) {
      const image = Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4//8/AwAI/AL+0I0YAAAAAElFTkSuQmCC",
        "base64",
      );
      return route.fulfill({ contentType: "image/png", body: image });
    }
    const match = path.match(/^\/api\/tickets\/(\d+)(?:\/(verify|flag|skip))?$/);
    if (match) {
      const ticket = tickets.find((t) => t.id === Number(match[1]));
      if (!ticket) return json(route, { detail: "Not found" }, 404);
      if (method === "PATCH") {
        Object.assign(ticket, request.postDataJSON());
        if (ticket.status === "verified") {
          ticket.status = "needs_review";
          ticket.tonnage_confirmed = false;
          ticket.cost = null;
        }
        log.patched.push(ticket.id);
        return json(route, { ...ticket });
      }
      if (method === "POST" && match[2] === "verify") {
        log.verified.push(ticket.id);
        ticket.status = "verified";
        ticket.tonnage_confirmed = true;
        ticket.price_snapshot_state = "priced";
        ticket.cost = "735.00";
        return json(route, { ...ticket });
      }
      if (method === "POST" && match[2] === "flag") {
        log.flagged.push(ticket.id);
        ticket.status = "flagged";
        ticket.tonnage_confirmed = false;
        ticket.price_snapshot_state = "pending";
        ticket.cost = null;
        return json(route, { ...ticket });
      }
      if (method === "POST" && match[2] === "skip") {
        ticket.status = "skipped";
        ticket.tonnage_confirmed = false;
        ticket.cost = null;
        return json(route, { ...ticket });
      }
    }
    return json(route, { detail: "Unmocked request: " + method + " " + path }, 404);
  });
  await page.goto("/upload?batch=5&ticket=" + deepLink);
  await expect(page.getByRole("heading", { name: "Original scanned ticket" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Extracted ticket information" })).toBeVisible();
  return { tickets, log };
}

test("AI data needs explicit human acceptance; accepting advances to the next ticket", async ({ page }) => {
  const { log } = await launch(page);
  await expect(page.getByText("0 / 2 resolved")).toBeVisible();
  expect(log.verified).toEqual([]);
  await page.getByRole("button", { name: "Accept & next" }).click();
  await expect(page.getByText("1 / 2 resolved")).toBeVisible();
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  expect(log.verified).toEqual([101]);
});

test("dirty edits are not silently dropped during ticket navigation", async ({ page }) => {
  const { log } = await launch(page);
  await page.getByLabel("Tonnage (tons)").fill("25.250");
  await expect(page.getByText(/Unsaved changes/)).toBeVisible();
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.getByText("Ticket 1 of 2")).toBeVisible();
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect.poll(() => log.patched.length).toBe(1);
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
});

test("flag defers approval and lets the contractor revisit the flagged record", async ({ page }) => {
  const { log } = await launch(page);
  await page.getByRole("button", { name: "Flag for later" }).click();
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  expect(log.flagged).toEqual([101]);
  expect(log.verified).toEqual([]);
  await page.getByRole("button", { name: "Flagged", exact: true }).click();
  await page.getByRole("button", { name: /#1 · 10482/ }).click();
  await expect(page.getByText("Ticket 1 of 2")).toBeVisible();
  await page.getByRole("button", { name: "Accept & next" }).click();
  await expect.poll(() => log.verified.length).toBe(1);
  expect(log.verified).toEqual([101]);
});

test("records can deep-link to a specific ticket in a batch", async ({ page }) => {
  await launch(page, 102);
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  await expect(page.getByLabel("Ticket number")).toHaveValue("10483");
});
