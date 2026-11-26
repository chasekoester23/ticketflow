// Field intake on a phone and a tablet, on a mocked API with fictional data: photograph
// tickets, choose the project, upload over a slow or dropped connection, find the upload
// again later, and check what was read. Delays are simulated with route timers.
import { expect, test } from "@playwright/test";
import { complete, json, mockWorkspace } from "./f_helpers.mjs";

const SIZES = [[390, 844, "phone"], [820, 1180, "tablet"]];
const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 0x10, 0x4a, 0x46, 0x49, 0x46, 0, 1]);
const photo = (n) => ({ name: `IMG_04${n}.jpg`, mimeType: "image/jpeg", buffer: JPEG });

const BATCH = { id: 7, project_id: 1, layout: "single", intake_mode: "manual", status: "uploaded", created_at: "2026-11-20T15:00:00Z",
  files: [{ id: 9, original_name: "IMG_0412.jpg", status: "uploaded", error: null }, { id: 10, original_name: "IMG_0413.jpg", status: "uploaded", error: null }] };
const QUEUE_ROW = { id: 7, project_id: 1, project_code: "24-115", project_name: "Fictional Road Job", status: "processing", intake_mode: "manual",
  created_at: "2026-11-20T15:00:00Z", created_by: "Fictional Owner", files: 2, failed_files: 0, expected_ticket_count: null, tickets: 0,
  needs_review: 0, flagged: 0, verified: 0, skipped: 0, failed: 0, faint_slots: 0 };

/** The upload endpoint, slow, and optionally dropping the first attempt mid-way. */
function uploads({ dropFirst = false } = {}) {
  const state = { attempts: 0, received: [] };
  state.extra = (request, path, method) => {
    if (path === "/api/batches" && method === "POST") {
      state.attempts += 1;
      const attempt = state.attempts;
      return async (route) => {
        await new Promise((resolve) => setTimeout(resolve, 1200));
        if (dropFirst && attempt === 1) return route.abort("internetdisconnected");
        state.received.push(request.postData() ?? "");
        return json(route, BATCH);
      };
    }
    if (path === "/api/batches/7") return (route) => json(route, { ...BATCH, status: "processing" });
    if (path === "/api/batches/7/tickets") return (route) => json(route, []);
    if (path === "/api/batches/7/manifest") return (route) => json(route, { detail: "n/a" }, 404);
    if (path === "/api/batches/queue" && state.received.length) return (route) => json(route, [QUEUE_ROW]);
    return null;
  };
  return state;
}

const noSidewaysScroll = async (page, where) => {
  const { scroll, client } = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  expect(scroll, `${where}: ${scroll}px wide in a ${client}px window`).toBeLessThanOrEqual(client);
};

for (const [width, height, size] of SIZES) {
  test.describe(`${size} ${width}x${height}`, () => {
    test.use({ viewport: { width, height }, hasTouch: true });

    test("photos taken one at a time go up together, to the chosen project, and the upload can be found again", async ({ page }) => {
      const server = uploads();
      await mockWorkspace(page, { extra: server.extra });
      await page.goto("/upload");
      await expect(page.getByRole("heading", { name: "Upload delivery tickets" })).toBeVisible();
      // The camera, one photo per press; the file picker still takes several at once.
      const camera = page.getByTestId("camera-input");
      await expect(camera).toHaveAttribute("capture", "environment");
      await expect(camera).toHaveAttribute("accept", /image\/jpeg/);
      await expect(page.getByRole("button", { name: "Take a photo of a ticket" })).toBeVisible();
      await camera.setInputFiles(photo(12));
      await camera.setInputFiles(photo(13));
      await expect(page.getByText("2 selected file(s)")).toBeVisible();
      // Nothing goes up until a project is chosen.
      const upload = page.getByRole("button", { name: "Upload for entry by hand" });
      await page.getByRole("combobox", { name: "Project" }).selectOption("");
      await expect(upload).toBeDisabled();
      await page.getByRole("combobox", { name: "Project" }).selectOption("1");
      await page.getByRole("combobox", { name: "Document layout" }).selectOption("single");
      await noSidewaysScroll(page, "upload form");
      await upload.click();
      // A slow connection: it says it's working, and can't be sent twice.
      await expect(page.getByRole("button", { name: "Uploading…" })).toBeDisabled();
      await expect(page.getByRole("heading", { name: /Batch #7/ })).toBeVisible();
      expect(server.received).toHaveLength(1);
      expect(server.received[0]).toContain('name="project_id"');
      expect(server.received[0]).toContain("IMG_0412.jpg");
      expect(server.received[0]).toContain("IMG_0413.jpg");
      await noSidewaysScroll(page, "batch status");
      // Leaving and coming back: the upload and its state are in the history.
      await page.goto("/upload");
      const row = page.getByRole("row").filter({ hasText: "#7" });
      await expect(row).toContainText("Being prepared");
      await expect(row.getByRole("link", { name: "Open batch 7" })).toBeVisible();
      await noSidewaysScroll(page, "upload history");
    });

    test("an interrupted upload says what happened, where the photos are, and goes again", async ({ page }) => {
      const server = uploads({ dropFirst: true });
      await mockWorkspace(page, { extra: server.extra });
      await page.goto("/upload");
      await page.getByTestId("camera-input").setInputFiles(photo(12));
      await page.getByRole("combobox", { name: "Project" }).selectOption("1");
      await page.getByRole("button", { name: "Upload for entry by hand" }).click();
      const alert = page.getByRole("alert").filter({ hasText: "The upload was interrupted" });
      await expect(alert).toBeVisible({ timeout: 10_000 });
      await expect(alert).toContainText("Check Upload history first");
      await expect(alert).toBeInViewport();
      await expect(page.getByText("1 selected file(s)")).toBeVisible();
      await page.getByRole("button", { name: "Upload for entry by hand" }).click();
      await expect(page.getByRole("heading", { name: /Batch #7/ })).toBeVisible();
      expect(server.attempts).toBe(2);
      expect(server.received).toHaveLength(1);
    });

    test("what was read can be checked and accepted by touch, without sideways scrolling", async ({ page }) => {
      const verified = [];
      await mockWorkspace(page, { extra: (request, path, method) => {
        if (path === "/api/batches/5/tickets") return (route) => json(route, [
          complete(1001, { batch_id: 5, tonnage: "64.000", flags: ["Tonnage unusually large (> 60)"], unit_price: "30.0000" }),
          complete(1002, { batch_id: 5 })]);
        if (path === "/api/tickets/1001/verify" && method === "POST") return (route) => { verified.push(1001); return json(route, complete(1001, { batch_id: 5, status: "verified", tonnage: "64.000" })); };
        return null;
      } });
      await page.goto("/upload?batch=5&ticket=1001");
      await expect(page.getByLabel("Ticket number")).toHaveValue("11001");
      await expect(page.getByLabel("Tonnage (tons)")).toHaveValue("64.000");
      const accept = page.getByRole("button", { name: /Accept & next/ });
      await accept.tap();
      expect(verified).toEqual([]);
      const check = page.getByTestId("confirm-tonnage");
      await check.scrollIntoViewIfNeeded();
      await check.tap();
      await accept.tap();
      await expect.poll(() => verified).toEqual([1001]);
      await noSidewaysScroll(page, "review");
      // The queue, its issue filter and the several-at-once bar fit too.
      // A phone starts with the queue tucked away; a tablet shows it.
      if (width < 768) await page.getByRole("button", { name: "Show queue" }).tap();
      await page.getByRole("button", { name: "Correct several…" }).tap();
      await expect(page.getByRole("group", { name: "Correct the chosen tickets together" })).toBeVisible();
      await noSidewaysScroll(page, "queue and bulk bar");
    });
  });
}
