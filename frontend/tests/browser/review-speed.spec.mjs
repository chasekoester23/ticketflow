// Review speed on a mocked API, with fictional tickets: where the cursor lands, how many
// key presses a ticket takes, the shortcuts, the queue's issue filter, corrections to many
// tickets at once, and scans fetched ahead. Timings here are a script's, never a person's.
import { expect, test } from "@playwright/test";
import { complete, cors, json, mockReview } from "./f_helpers.mjs";

const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4//8/AwAI/AL+0I0YAAAAAElFTkSuQmCC", "base64");
const accept = (page) => page.getByRole("button", { name: /Accept & next/ });
const warned = (id, fields = {}) => complete(id, { tonnage: "64.000", flags: ["Tonnage unusually large (> 60)"], ...fields });
/** A person glancing at the scan. Enter on Accept is ignored in a ticket's first 350 ms. */
const look = (page) => page.waitForTimeout(400);

/** Key presses, counted as a person would press them: a chord is one. */
function keyCounter(page) {
  const counter = { keys: 0, press: async (key) => { counter.keys += 1; await page.keyboard.press(key); } };
  return counter;
}

test("a clean AI-read ticket opens on Accept, and one key approves it and opens the next", async ({ page }) => {
  const tickets = [101, 102, 103, 104, 105].map((id) => complete(id));
  const log = await mockReview(page, tickets, { intake: "extract" });
  await page.goto("/upload?batch=5&ticket=101");
  const counter = keyCounter(page);
  for (let n = 1; n <= 4; n += 1) {
    await expect(page.getByText(`Ticket ${n} of 5`)).toBeVisible();
    await expect(accept(page)).toBeFocused();
    await look(page);
    await counter.press("Enter");
    await expect(page.getByText(`Ticket ${n + 1} of 5`)).toBeVisible();
  }
  expect(log.verified).toEqual([101, 102, 103, 104]);
  expect(counter.keys / 4).toBe(1);
});

test("a value the reader warned about can't be approved unseen; Enter on it is the look", async ({ page }) => {
  const log = await mockReview(page, [warned(101), complete(102)], { intake: "extract" });
  await page.goto("/upload?batch=5&ticket=101");
  const tons = page.getByLabel("Tonnage (tons)");
  // The cursor starts on what needs a person, not on Accept.
  await expect(tons).toBeFocused();
  await expect(page.getByTestId("accept-checks")).toContainText("tons");
  await look(page);
  // The approval chord doesn't wave it through.
  await page.keyboard.press("Control+Enter");
  await expect(tons).toBeFocused();
  await expect(page.getByTestId("check-tonnage")).toHaveClass(/tf-rv-check-asked/);
  await page.waitForTimeout(150);
  expect(log.verified).toEqual([]);
  // Looked at: Enter confirms it and goes to Accept; Enter there approves.
  const counter = keyCounter(page);
  await counter.press("Enter");
  await expect(accept(page)).toBeFocused();
  await expect(page.getByTestId("checked-tonnage")).toBeVisible();
  await counter.press("Enter");
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  expect(log.verified).toEqual([101]);
  expect(counter.keys).toBe(2);
  // Nothing was changed on the way: the server got the approval alone.
  expect(log.patched).toEqual([]);
});

test("on a touch screen the warned value is confirmed with a button, and the server still decides", async ({ page }) => {
  const log = await mockReview(page, [warned(101, { number_warning: "This may be the job number." }), complete(102)], { intake: "extract" });
  await page.goto("/upload?batch=5&ticket=101");
  await accept(page).click();
  await expect(page.getByLabel("Ticket number")).toBeFocused();
  expect(log.verified).toEqual([]);
  await page.getByTestId("confirm-ticket_number").click();
  await page.getByTestId("confirm-tonnage").click();
  await accept(page).click();
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  expect(log.verified).toEqual([101]);
});

test("correcting a warned value counts as looking at it", async ({ page }) => {
  const log = await mockReview(page, [warned(101), complete(102)], { intake: "extract" });
  await page.goto("/upload?batch=5&ticket=101");
  await page.getByLabel("Tonnage (tons)").fill("24.000");
  await page.keyboard.press("Control+Enter");
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  expect(log.patched[0].body).toEqual({ tonnage: "24.000" });
  expect(log.verified).toEqual([101]);
});

test("a held Enter approves one ticket, not the next one as it opens", async ({ page }) => {
  const log = await mockReview(page, [complete(101), complete(102), complete(103)], { intake: "extract" });
  await page.goto("/upload?batch=5&ticket=101");
  await expect(accept(page)).toBeFocused();
  await look(page);
  await page.keyboard.down("Enter");
  await expect(page.getByText("Ticket 2 of 3")).toBeVisible();
  await expect(accept(page)).toBeFocused();
  await look(page);
  await page.keyboard.down("Enter"); // the key repeating
  await page.keyboard.up("Enter");
  await page.waitForTimeout(200);
  expect(log.verified).toEqual([101]);
  await expect(page.getByText("Ticket 2 of 3")).toBeVisible();
});

test("blank fields still come first, and Enter skips the ones already read cleanly", async ({ page }) => {
  await mockReview(page, [complete(101, { material_type: null, supplier: null })], { intake: "extract" });
  await page.goto("/upload?batch=5&ticket=101");
  await expect(page.getByLabel("Material")).toBeFocused();
  await page.keyboard.type("Sand");
  await page.keyboard.press("Enter");
  // Tons were read and nothing warned about them: straight on to the next blank.
  await expect(page.getByLabel("Supplier")).toBeFocused();
  await page.keyboard.type("Example Aggregates");
  await page.keyboard.press("Enter");
  await expect(accept(page)).toBeFocused();
});

test("shortcuts: flag, skip, next ticket with an issue, rotate both ways and fit, all listed under ?", async ({ page }) => {
  const tickets = [complete(101), complete(102), complete(103, { unit_price: "30.0000", duplicates: ["Same number as ticket #7"] }), complete(104)];
  const log = await mockReview(page, tickets, { intake: "extract" });
  await page.goto("/upload?batch=5&ticket=101");
  await expect(accept(page)).toBeFocused();
  await page.keyboard.press("?");
  const dialog = page.getByRole("dialog", { name: "Keyboard shortcuts" });
  for (const text of ["Enter on Accept", "Next open ticket with an issue", "Flag this ticket for later", "Skip this as not a ticket", "Rotate the view right or left", "fit to width"]) {
    await expect(dialog).toContainText(text);
  }
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);

  const image = page.getByTestId("scan-box").locator("img");
  await page.keyboard.press("Shift+R");
  await expect(image).toHaveAttribute("style", /rotate\(270deg\)/);
  await page.keyboard.press("r");
  await expect(image).toHaveAttribute("style", /rotate\(0deg\)/);
  await page.keyboard.press("+");
  await expect(page.getByTestId("zoom-level")).toHaveText("125%");
  await page.getByRole("button", { name: /Fit to width/ }).click();
  await expect(page.getByTestId("zoom-level")).toHaveText("100%");

  // I: the next open ticket with an issue (here, a possible duplicate).
  const issue = page.getByLabel("Show only tickets with an issue");
  await issue.selectOption("duplicate");
  await issue.blur();
  await page.keyboard.press("i");
  await expect(page.getByText("Ticket 3 of 4")).toBeVisible();

  // X flags the ticket on screen, with the note typed.
  await page.getByLabel("Note if you set this aside (optional)").fill("Ask the plant");
  await page.keyboard.press("Escape");
  await page.keyboard.press("x");
  await expect.poll(() => log.flagged).toEqual([{ id: 103, note: "Ask the plant" }]);

  // S asks first; saying no skips nothing.
  await expect(page.getByText("Ticket 4 of 4")).toBeVisible();
  await page.keyboard.press("Escape");
  page.once("dialog", (d) => d.dismiss());
  await page.keyboard.press("s");
  await page.waitForTimeout(150);
  expect(log.skipped).toEqual([]);
  page.once("dialog", (d) => d.accept());
  await page.keyboard.press("s");
  await expect.poll(() => log.skipped.length).toBe(1);
  expect(log.skipped).toEqual([104]);
  expect(log.verified).toEqual([]);
});

test("the queue narrows to one kind of issue, says how many are open, and Accept stays within it", async ({ page }) => {
  const tickets = [
    complete(101, { unit_price: "30.0000" }), complete(102), complete(103, { unit_price: "30.0000" }), complete(104),
    complete(105, { unit_price: "30.0000", tonnage: null }), complete(106, { unit_price: "30.0000", unit: "cy" }),
  ];
  const log = await mockReview(page, tickets, { intake: "extract" });
  await page.goto("/upload?batch=5&ticket=101");
  const issue = page.getByLabel("Show only tickets with an issue");
  await expect(issue.locator("option", { hasText: "No rate" })).toHaveText("No rate (2 open)");
  await expect(issue.locator("option", { hasText: "Missing tons" })).toHaveText("Missing tons (1 open)");
  await expect(issue.locator("option", { hasText: "Unit not tons" })).toHaveText("Unit not tons (1 open)");
  await issue.selectOption("no_rate");
  const queue = page.getByRole("region", { name: "Ticket queue" });
  await expect(queue.getByRole("button", { name: /^#\d/ })).toHaveCount(2);
  await expect(queue.getByRole("button", { name: /#2 · 10102/ })).toBeVisible();
  await queue.getByRole("button", { name: /#2 · 10102/ }).click();
  await expect(accept(page)).toBeFocused();
  await look(page);
  await page.keyboard.press("Enter");
  // The next ticket with no rate, not simply the next one.
  await expect(page.getByText("Ticket 4 of 6")).toBeVisible();
  expect(log.verified).toEqual([102]);
});

test("progress shows resolved of total, and a time left estimate from this session's own pace", async ({ page }) => {
  await mockReview(page, [101, 102, 103, 104, 105, 106].map((id) => complete(id)), { intake: "extract" });
  await page.goto("/upload?batch=5&ticket=101");
  for (let n = 1; n <= 3; n += 1) {
    await expect(page.getByText(`Ticket ${n} of 6`)).toBeVisible();
    await look(page);
    await page.keyboard.press("Enter");
  }
  await expect(page.getByText("3 / 6 resolved")).toBeVisible();
  await expect(page.getByTestId("time-left")).toContainText("left for 3 open at this pace (an estimate)");
});

test("one supplier onto several unapproved tickets: approved ones are never sent, nothing is approved", async ({ page }) => {
  const tickets = [complete(101, { supplier: "Exmple Agg" }), complete(102, { supplier: "Exmple Agg" }), complete(103, { status: "verified" }), complete(104, { supplier: "Exmple Agg", status: "flagged" })];
  const bulk = [];
  const log = await mockReview(page, tickets, { intake: "extract", intercept: (request, path) => path === "/api/tickets/bulk-edit" ? (route) => {
    const body = request.postDataJSON();
    bulk.push(body);
    body.ticket_ids.forEach((id) => { const t = tickets.find((x) => x.id === id); t[body.field] = body.value; t.revision += "b"; });
    return json(route, { changed: body.ticket_ids.length, unchanged: 0, left_alone: {} });
  } : null });
  await page.goto("/upload?batch=5&ticket=101");
  const queue = page.getByRole("region", { name: "Ticket queue" });
  await queue.getByRole("button", { name: "All", exact: true }).click();
  await queue.getByRole("button", { name: "Correct several…" }).click();
  await expect(queue.getByRole("checkbox", { name: /Select ticket 3/ })).toBeDisabled();
  await queue.getByRole("button", { name: "Choose all 3 shown" }).click();
  const bar = page.getByRole("group", { name: "Correct the chosen tickets together" });
  await expect(bar.getByTestId("bulk-count")).toHaveText("3 chosen");
  await bar.getByLabel("To").fill("Example Aggregates");
  await bar.getByRole("button", { name: "Apply to 3 tickets" }).click();
  await expect(bar.getByTestId("bulk-result")).toContainText("Changed 3. Each still needs its own Accept.");
  expect(bulk).toEqual([{ ticket_ids: [101, 102, 104], field: "supplier", value: "Example Aggregates" }]);
  await expect(page.locator("#review-supplier")).toHaveValue("Example Aggregates");
  expect(log.verified).toEqual([]);
});

test("the next scans are fetched once, ahead, even though the API forbids caching them", async ({ page }) => {
  const hits = {};
  await mockReview(page, [101, 102, 103, 104].map((id) => complete(id)), { intake: "extract", intercept: (request, path) => {
    const m = path.match(/\/tickets\/(\d+)\/image$/);
    if (!m) return null;
    hits[m[1]] = (hits[m[1]] ?? 0) + 1;
    return async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 300));
      await route.fulfill({ contentType: "image/png", headers: { ...cors, "cache-control": "private, no-store" }, body: PNG });
    };
  } });
  await page.goto("/upload?batch=5&ticket=101");
  for (let n = 1; n <= 3; n += 1) {
    await expect(page.getByText(`Ticket ${n} of 4`)).toBeVisible();
    await page.waitForTimeout(700);
    await page.keyboard.press("Enter");
    // Ready at once: the scan was already here.
    await expect(page.getByTestId("scan-box").locator(`img[data-ticket="${101 + n}"]`)).toBeVisible({ timeout: 250 });
  }
  expect(Object.values(hits).every((count) => count === 1)).toBe(true);
});
