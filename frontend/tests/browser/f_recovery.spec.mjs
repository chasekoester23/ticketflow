import { expect, test } from "@playwright/test";
import { complete, draftKeys, mockReview, ticket, json } from "./f_helpers.mjs";

const REVIEW = "/upload?batch=5&ticket=101";

test("typed values survive a closed tab, come back unsaved, and say so", async ({ page, context }) => {
  const tickets = [ticket(101), ticket(102)];
  const log = await mockReview(page, tickets);
  await page.goto(REVIEW);
  await page.getByLabel("Ticket number").fill("010482");
  await page.getByLabel("Tonnage (tons)").fill("24.5");
  await page.getByLabel("Note if you set this aside (optional)").fill("Tons smudged");
  await expect.poll(() => draftKeys(page)).toEqual(["ticketflow.draft.101.r101-1"]);
  await page.close();

  const again = await context.newPage();
  const logAgain = await mockReview(again, tickets);
  await again.goto(REVIEW);
  await expect(again.getByTestId("draft-restored")).toContainText("Restored unsaved work");
  await expect(again.getByTestId("draft-restored")).toContainText("isn't saved yet");
  await expect(again.getByLabel("Ticket number")).toHaveValue("010482");
  await expect(again.getByLabel("Tonnage (tons)")).toHaveValue("24.5");
  await expect(again.getByLabel("Note if you set this aside (optional)")).toHaveValue("Tons smudged");
  await expect(again.getByRole("status").getByText(/Unsaved changes/)).toBeVisible();
  // Nothing went to the server, on either visit, until a person acts.
  expect(log.patched).toEqual([]);
  expect(logAgain.patched).toEqual([]);
  expect(logAgain.verified).toEqual([]);

  await again.getByRole("button", { name: "Save changes" }).click();
  await expect.poll(() => logAgain.patched.length).toBe(1);
  expect(logAgain.patched[0].body).toEqual({ ticket_number: "010482", tonnage: "24.5" });
});

test("typed values survive the browser tab actually crashing", async ({ page, context }) => {
  const tickets = [ticket(101)];
  await mockReview(page, tickets);
  await page.goto(REVIEW);
  await page.getByLabel("Ticket number").fill("777");
  await expect.poll(() => draftKeys(page)).toHaveLength(1);
  const crashed = new Promise((resolve) => page.once("crash", resolve));
  const devtools = await context.newCDPSession(page);
  devtools.send("Page.crash").catch(() => undefined);
  await crashed;

  const again = await context.newPage();
  await mockReview(again, tickets);
  await again.goto(REVIEW);
  await expect(again.getByTestId("draft-restored")).toBeVisible();
  await expect(again.getByLabel("Ticket number")).toHaveValue("777");
});

test("one click discards a restored draft, for good", async ({ page, context }) => {
  const tickets = [ticket(101, { tonnage: "20.000" })];
  await mockReview(page, tickets);
  await page.goto(REVIEW);
  await page.getByLabel("Tonnage (tons)").fill("99");
  await expect.poll(() => draftKeys(page)).toHaveLength(1);
  await page.reload();
  await expect(page.getByLabel("Tonnage (tons)")).toHaveValue("99");
  await page.getByRole("button", { name: "Discard it" }).click();
  await expect(page.getByLabel("Tonnage (tons)")).toHaveValue("20.000");
  await expect(page.getByTestId("draft-restored")).toHaveCount(0);
  await expect(page.getByRole("status").getByText(/Unsaved changes/)).toHaveCount(0);
  await expect.poll(() => draftKeys(page)).toEqual([]);
  await page.reload();
  await expect(page.getByLabel("Tonnage (tons)")).toHaveValue("20.000");
  await expect(page.getByTestId("draft-restored")).toHaveCount(0);
  expect(context.pages().length).toBe(1);
});

test("if the ticket changed since, typed values aren't applied: they sit beside the current ones", async ({ page }) => {
  const tickets = [complete(101, { tonnage: "20.000", supplier: "Example Aggregates" })];
  const log = await mockReview(page, tickets);
  await page.goto(REVIEW);
  await page.getByLabel("Tonnage (tons)").fill("25.000");
  await page.getByLabel("Supplier").fill("Sample Quarry");
  await expect.poll(() => draftKeys(page)).toHaveLength(1);

  // A teammate corrects the ticket while this browser is closed.
  tickets[0].tonnage = "22.000";
  tickets[0].revision = "r101-2";
  await page.reload();
  const panel = page.getByTestId("draft-conflict");
  await expect(panel).toContainText("This ticket changed after you typed these values");
  await expect(panel.getByRole("row", { name: /Tonnage/ })).toContainText("25.000");
  await expect(panel.getByRole("row", { name: /Tonnage/ })).toContainText("22.000");
  // The form shows the current values, and nothing counts as unsaved yet.
  await expect(page.getByLabel("Tonnage (tons)")).toHaveValue("22.000");
  await expect(page.getByLabel("Supplier")).toHaveValue("Example Aggregates");
  await expect(page.getByTestId("draft-restored")).toHaveCount(0);
  await expect(page.getByRole("status").getByText(/Unsaved changes/)).toHaveCount(0);

  // One field at a time: this person's supplier, the teammate's tons.
  await panel.getByRole("button", { name: "Use mine for Supplier" }).click();
  await expect(page.getByLabel("Supplier")).toHaveValue("Sample Quarry");
  await expect(page.getByLabel("Tonnage (tons)")).toHaveValue("22.000");
  await expect(page.getByRole("status").getByText(/Unsaved changes/)).toBeVisible();
  expect(log.patched).toEqual([]);
  await panel.getByRole("button", { name: "Discard what I typed" }).click();
  await expect(panel).toHaveCount(0);
  await expect(page.getByLabel("Supplier")).toHaveValue("Sample Quarry");
  await expect.poll(() => draftKeys(page)).toEqual(["ticketflow.draft.101.r101-2"]);
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect.poll(() => log.patched.length).toBe(1);
  expect(log.patched[0].body).toEqual({ supplier: "Sample Quarry" });
  await expect.poll(() => draftKeys(page)).toEqual([]);
});

test("a refused stale save keeps what was typed, for a person to compare", async ({ page }) => {
  const tickets = [complete(101, { tonnage: "20.000" })];
  const log = await mockReview(page, tickets);
  await page.goto(REVIEW);
  await expect(page.getByLabel("Tonnage (tons)")).toHaveValue("20.000");
  await page.getByLabel("Tonnage (tons)").fill("25.000");
  tickets[0].tonnage = "22.000";
  tickets[0].revision = "r101-2";
  await page.getByRole("button", { name: "Accept & next" }).click();
  await expect(page.getByTestId("stale-notice")).toContainText("Someone else changed this ticket");
  await expect(page.getByLabel("Tonnage (tons)")).toHaveValue("22.000");
  await expect(page.getByTestId("draft-conflict").getByRole("row", { name: /Tonnage/ })).toContainText("25.000");
  expect(log.verified).toEqual([]);
  await page.getByTestId("draft-conflict").getByRole("button", { name: "Use all I typed" }).click();
  await expect(page.getByLabel("Tonnage (tons)")).toHaveValue("25.000");
  await expect(page.getByTestId("draft-conflict")).toHaveCount(0);
  expect(log.patched).toEqual([]);
});

for (const [action, click] of [
  ["save", (page) => page.getByRole("button", { name: "Save changes" }).click()],
  ["accept", (page) => page.getByRole("button", { name: "Accept & next" }).click()],
  ["flag", (page) => page.getByRole("button", { name: "Flag for later" }).click()],
  ["skip", async (page) => {
    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "Skip non-ticket" }).click();
  }],
]) {
  test(`${action} clears the draft`, async ({ page }) => {
    const tickets = [complete(101), complete(102)];
    await mockReview(page, tickets);
    await page.goto(REVIEW);
    await page.getByLabel("Tonnage (tons)").fill("21.000");
    await expect.poll(() => draftKeys(page)).toHaveLength(1);
    await click(page);
    await expect.poll(() => draftKeys(page)).toEqual([]);
  });
}

test("choosing to discard on the way to another ticket really discards", async ({ page }) => {
  await mockReview(page, [ticket(101), ticket(102)]);
  await page.goto(REVIEW);
  await page.getByLabel("Ticket number").fill("10482");
  await expect.poll(() => draftKeys(page)).toHaveLength(1);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  await expect.poll(() => draftKeys(page)).toEqual([]);
  await page.getByRole("button", { name: "Previous", exact: true }).click();
  await expect(page.getByLabel("Ticket number")).toHaveValue("");
  await expect(page.getByTestId("draft-restored")).toHaveCount(0);
});

test("signing out clears every draft on this computer", async ({ page }) => {
  await page.addInitScript(() => {
    if (!window.localStorage.getItem("seeded")) {
      window.localStorage.setItem("seeded", "1");
      window.localStorage.setItem("ticketflow.draft.555.r1", JSON.stringify({ ticketId: 555, revision: "r1", accountId: 1, savedAt: "2026-11-01T00:00:00Z", typed: { tonnage: "1" } }));
    }
  });
  await mockReview(page, [ticket(101)]);
  await page.goto(REVIEW);
  await page.getByLabel("Ticket number").fill("10482");
  await expect.poll(async () => (await draftKeys(page)).length).toBe(2);
  let asked = "";
  page.on("dialog", (dialog) => { if (dialog.type() === "confirm") asked = dialog.message(); dialog.accept(); });
  await page.route("**/api/auth/me", (route) => json(route, { detail: "Sign in" }, 401));
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);
  expect(asked).toContain("clears unsaved ticket work");
  expect(await draftKeys(page)).toEqual([]);
});

test("someone else's leftover draft is never offered, and is removed when they sign in", async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem("ticketflow.draft.101.r101-1", JSON.stringify({ ticketId: 101, revision: "r101-1", accountId: 2, savedAt: "2026-11-01T00:00:00Z", typed: { ticket_number: "THEIRS" } }));
  });
  await mockReview(page, [ticket(101)], { account: { id: 7 } });
  await page.goto(REVIEW);
  await expect(page.getByText("Ticket 1 of 1")).toBeVisible();
  await expect(page.getByLabel("Ticket number")).toHaveValue("");
  await expect(page.getByTestId("draft-restored")).toHaveCount(0);
});

test("a viewer's page neither restores nor keeps drafts", async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem("ticketflow.draft.101.r101-1", JSON.stringify({ ticketId: 101, revision: "r101-1", accountId: 1, savedAt: "2026-11-01T00:00:00Z", typed: { ticket_number: "OLD" } }));
  });
  await mockReview(page, [ticket(101)], { role: "viewer" });
  await page.goto(REVIEW);
  await expect(page.getByText("Ticket 1 of 1")).toBeVisible();
  await expect(page.getByLabel("Ticket number")).toHaveValue("");
  await expect(page.getByTestId("draft-restored")).toHaveCount(0);
});

for (const [name, script] of [
  ["throws on every use", () => {
    for (const method of ["getItem", "setItem", "removeItem", "key"]) Storage.prototype[method] = () => { throw new Error("blocked"); };
  }],
  ["is missing altogether", () => {
    Object.defineProperty(window, "localStorage", { get() { throw new DOMException("denied", "SecurityError"); } });
  }],
  ["is full", () => {
    Storage.prototype.setItem = () => { throw new DOMException("full", "QuotaExceededError"); };
  }],
]) {
  test(`review still works when browser storage ${name}`, async ({ page }) => {
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.addInitScript(script);
    const log = await mockReview(page, [complete(101), complete(102)]);
    await page.goto(REVIEW);
    await page.getByLabel("Tonnage (tons)").fill("21.000");
    await page.getByRole("button", { name: "Accept & next" }).click();
    await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
    expect(log.verified).toEqual([101]);
    expect(errors).toEqual([]);
  });
}
