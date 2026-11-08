import { expect, test } from "@playwright/test";
import { complete, draftKeys, json, mockReview, ticket } from "./f_helpers.mjs";

const REVIEW = "/upload?batch=5&ticket=101";

/** Fails the matching request, a set number of times, then lets it through. */
function failing(pattern, times, answer) {
  let left = times;
  return (request, path, method) => {
    if (!pattern.test(`${method} ${path}`) || left <= 0) return null;
    left -= 1;
    return answer;
  };
}

const problems = [
  ["503", (route) => json(route, { detail: "The database is busy or unavailable. Nothing was saved. Try again in a moment." }, 503, { "Retry-After": "5", "X-Request-ID": "ref503" }),
    "busy or temporarily unavailable", "in about 5 seconds", "ref503"],
  ["429", (route) => json(route, { detail: "Slow down." }, 429, { "Retry-After": "30", "X-Request-ID": "ref429" }),
    "too many requests", "Wait about 30 seconds", "ref429"],
  ["a lost connection", (route) => route.abort("internetdisconnected"),
    "couldn't reach the server", "Check your connection", null],
  ["a 500 that gives a reference", (route) => json(route, { detail: "Something went wrong on our side, and nothing further was changed. If it keeps happening, quote reference abc123def456.", request_id: "abc123def456" }, 500),
    "something went wrong on the server", "quote the reference", "abc123def456"],
];

for (const [name, answer, cause, advice, reference] of problems) {
  test(`${name} during a save keeps what was typed, explains, and offers a retry`, async ({ page }) => {
    const tickets = [ticket(101), ticket(102)];
    const log = await mockReview(page, tickets, { intercept: failing(/^PATCH \/api\/tickets\/101$/, 1, answer) });
    await page.goto(REVIEW);
    await page.getByLabel("Ticket number").fill("010482");
    await page.getByLabel("Tonnage (tons)").fill("24.5");
    await page.getByRole("button", { name: "Save changes" }).click();

    const failure = page.getByTestId("action-failure");
    await expect(failure).toContainText(cause);
    await expect(failure).toContainText(advice);
    await expect(failure).toContainText("What you typed is still here.");
    if (reference) await expect(failure).toContainText(`Reference: ${reference}`);
    else await expect(failure).not.toContainText("Reference");
    await expect(page.getByLabel("Ticket number")).toHaveValue("010482");
    await expect(page.getByLabel("Tonnage (tons)")).toHaveValue("24.5");
    await expect(page.getByRole("status").getByText(/Unsaved changes/)).toBeVisible();
    expect(await draftKeys(page)).toHaveLength(1);
    expect(log.patched).toEqual([]);

    await failure.getByRole("button", { name: "Try again" }).click();
    await expect(failure).toHaveCount(0);
    await expect.poll(() => log.patched.length).toBe(1);
    expect(log.patched[0].body).toEqual({ ticket_number: "010482", tonnage: "24.5" });
    await expect.poll(() => draftKeys(page)).toEqual([]);
  });
}

test("an approval that fails after its corrections saved says exactly that, and a retry only approves", async ({ page }) => {
  const tickets = [complete(101), complete(102)];
  const log = await mockReview(page, tickets, {
    intercept: failing(/^POST \/api\/tickets\/101\/verify$/, 1, (route) => json(route, { detail: "Busy" }, 503)),
  });
  await page.goto(REVIEW);
  await page.getByLabel("Tonnage (tons)").fill("24.500");
  await page.getByRole("button", { name: "Accept & next" }).click();

  const failure = page.getByTestId("action-failure");
  await expect(failure).toContainText("Your changes were saved, but this approval did not go through");
  await expect(page.getByLabel("Tonnage (tons)")).toHaveValue("24.500");
  await expect(page.getByText("Ticket 1 of 2")).toBeVisible();
  expect(log.patched).toHaveLength(1);
  expect(log.verified).toEqual([]);

  await failure.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  expect(log.patched).toHaveLength(1);
  expect(log.verified).toEqual([101]);
  expect(log.ifMatch.at(-1)).toBe("r101-2");
});

test("a failed flag keeps its note through the save that came first, and the retry sends it", async ({ page }) => {
  const tickets = [complete(101), complete(102)];
  const log = await mockReview(page, tickets, {
    intercept: failing(/^POST \/api\/tickets\/101\/flag$/, 1, (route) => route.abort("connectionreset")),
  });
  await page.goto(REVIEW);
  await page.getByLabel("Tonnage (tons)").fill("24.500");
  await page.getByLabel("Note if you set this aside (optional)").fill("Ask the driver");
  await page.getByRole("button", { name: "Flag for later" }).click();
  const failure = page.getByTestId("action-failure");
  await expect(failure).toContainText("Your changes were saved");
  await expect(page.getByLabel("Note if you set this aside (optional)")).toHaveValue("Ask the driver");
  await failure.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  expect(log.flagged).toEqual([{ id: 101, note: "Ask the driver" }]);
});

test("a refusal the server explained is shown as written, with no pointless retry", async ({ page }) => {
  const log = await mockReview(page, [complete(101), complete(102)], {
    intercept: failing(/^POST \/api\/tickets\/101\/verify$/, 1, (route) => json(route, { detail: "Resolve ticket issues before verification: Date is in the future" }, 422)),
  });
  await page.goto(REVIEW);
  await page.getByRole("button", { name: "Accept & next" }).click();
  const failure = page.getByTestId("action-failure");
  await expect(failure).toHaveText(/Date is in the future/);
  await expect(failure.getByRole("button", { name: "Try again" })).toHaveCount(0);
  await failure.getByRole("button", { name: "Dismiss" }).click();
  await expect(failure).toHaveCount(0);
  expect(log.verified).toEqual([]);
});

test("a session that ended says so, and the typed values wait on this computer", async ({ page }) => {
  await mockReview(page, [ticket(101)], {
    intercept: failing(/^PATCH \/api\/tickets\/101$/, 1, (route) => json(route, { detail: "Sign in to continue." }, 401)),
  });
  await page.goto(REVIEW);
  await page.getByLabel("Ticket number").fill("10482");
  await page.getByRole("button", { name: "Save changes" }).click();
  const failure = page.getByTestId("action-failure");
  await expect(failure).toContainText("you've been signed out");
  await expect(failure).toContainText("kept on this computer");
  await expect(failure.getByRole("link", { name: "Sign in again" })).toHaveAttribute("href", "/login");
  await expect(failure.getByRole("button", { name: "Try again" })).toHaveCount(0);
  await expect(page.getByLabel("Ticket number")).toHaveValue("10482");
  expect(await draftKeys(page)).toHaveLength(1);
});

test("the failure goes when the person moves to another ticket", async ({ page }) => {
  await mockReview(page, [complete(101), complete(102)], {
    intercept: failing(/^POST \/api\/tickets\/101\/verify$/, 1, (route) => json(route, { detail: "Busy" }, 503)),
  });
  await page.goto(REVIEW);
  await page.getByRole("button", { name: "Accept & next" }).click();
  await expect(page.getByTestId("action-failure")).toBeVisible();
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  await expect(page.getByTestId("action-failure")).toHaveCount(0);
});

/** Holds each matching request until released, to look at the moment an answer is pending. */
function holding(pattern) {
  const held = [];
  const intercept = (request, path, method) => pattern.test(`${method} ${path}`)
    ? (route) => new Promise((resolve) => { held.push(() => { route.fallback().then(resolve); }); })
    : null;
  return { intercept, held };
}

test("two clicks in the same instant submit once", async ({ page }) => {
  const tickets = [complete(101), complete(102)];
  const log = await mockReview(page, tickets);
  await page.goto(REVIEW);
  await page.getByLabel("Tonnage (tons)").fill("24.500");
  await page.evaluate(() => {
    const button = [...document.querySelectorAll("button")].find((b) => b.textContent.includes("Accept & next"));
    button.click();
    button.click();
  });
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  await page.waitForTimeout(150);
  expect(log.patched).toHaveLength(1);
  expect(log.verified).toEqual([101]);
});

test("a second click between the save and the approval doesn't approve twice", async ({ page }) => {
  const tickets = [complete(101), complete(102)];
  const verifies = [];
  const log = await mockReview(page, tickets, {
    intercept: (request, path, method) => {
      if (method !== "POST" || path !== "/api/tickets/101/verify") return null;
      verifies.push(request.headers()["if-match"]);
      return async (route) => {
        if (verifies.length === 1) await new Promise((resolve) => setTimeout(resolve, 400));
        tickets[0].status = "verified";
        await json(route, { ...tickets[0] });
      };
    },
  });
  await page.goto(REVIEW);
  await page.getByLabel("Tonnage (tons)").fill("24.500");
  const accept = page.getByRole("button", { name: "Accept & next" });
  await accept.click();
  await expect.poll(() => verifies.length).toBe(1);
  // The save has answered and the approval is on its way.
  await page.keyboard.press("Control+Enter");
  await page.evaluate(() => {
    [...document.querySelectorAll("button")].find((b) => b.textContent.includes("Accept & next"))?.click();
  });
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  await page.waitForTimeout(150);
  expect(verifies).toHaveLength(1);
  expect(log.patched).toHaveLength(1);
});

test("while a save is on its way the keyboard can't move to another ticket", async ({ page }) => {
  const tickets = [complete(101), complete(102), complete(103)];
  const { intercept, held } = holding(/^PATCH \/api\/tickets\/101$/);
  const log = await mockReview(page, tickets, { intercept });
  await page.goto(REVIEW);
  await page.getByLabel("Tonnage (tons)").fill("24.500");
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect.poll(() => held.length).toBe(1);
  await page.keyboard.press("Alt+ArrowDown");
  await page.waitForTimeout(100);
  await expect(page.getByText("Ticket 1 of 3")).toBeVisible();
  expect(log.requests.filter((r) => r === "PATCH /api/tickets/101")).toHaveLength(1);
});

test("going offline says what still works", async ({ page, context }) => {
  await mockReview(page, [ticket(101)]);
  await page.goto(REVIEW);
  await expect(page.getByLabel("Ticket number")).toBeVisible();
  await expect(page.getByTestId("offline-notice")).toHaveCount(0);
  await context.setOffline(true);
  await expect(page.getByTestId("offline-notice")).toContainText("lost its connection");
  await context.setOffline(false);
  await expect(page.getByTestId("offline-notice")).toHaveCount(0);
});
