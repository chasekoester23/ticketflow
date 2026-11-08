import { expect, test } from "@playwright/test";
import { complete, mockReview, ticket } from "./f_helpers.mjs";

const flagged = (id, note = "Ask the driver") => complete(id, { status: "flagged", flag_note: note });
const verified = (id) => complete(id, { status: "verified" });

/** Once the ticket is on screen, out of every field, so single keys reach the page. */
async function leaveField(page) {
  await expect(page.getByTestId("position")).toBeVisible();
  await page.keyboard.press("Escape");
}

test("N goes to the next unreviewed ticket, skipping what's done or set aside, and wraps", async ({ page }) => {
  await mockReview(page, [complete(101), verified(102), flagged(103), complete(104), verified(105)]);
  await page.goto("/upload?batch=5&ticket=101");
  await expect(page.getByText("Ticket 1 of 5")).toBeVisible();
  await leaveField(page);
  await page.keyboard.press("n");
  await expect(page.getByText("Ticket 4 of 5")).toBeVisible();
  await page.keyboard.press("n");
  await expect(page.getByText("Ticket 1 of 5")).toBeVisible();
});

test("F goes to the next flagged ticket and shows the flagged queue", async ({ page }) => {
  await mockReview(page, [complete(101), flagged(102), complete(103), flagged(104, "Tons smudged")]);
  await page.goto("/upload?batch=5&ticket=101");
  await leaveField(page);
  await page.keyboard.press("f");
  await expect(page.getByText("Ticket 2 of 4")).toBeVisible();
  await expect(page.getByRole("button", { name: "Flagged", exact: true })).toHaveAttribute("aria-pressed", "true");
  await page.keyboard.press("f");
  await expect(page.getByText("Ticket 4 of 4")).toBeVisible();
  await expect(page.getByTestId("flag-note")).toContainText("Tons smudged");
});

test("when there's nothing left to jump to, it says so and stays put", async ({ page }) => {
  await mockReview(page, [complete(101), verified(102)]);
  await page.goto("/upload?batch=5&ticket=101");
  await leaveField(page);
  await page.keyboard.press("f");
  await expect(page.getByTestId("jump-status")).toHaveText("No flagged tickets in this batch.");
  await page.keyboard.press("n");
  await expect(page.getByTestId("jump-status")).toHaveText("No other ticket in this batch needs review.");
  await expect(page.getByText("Ticket 1 of 2")).toBeVisible();
});

test("N and F typed into a field are just letters", async ({ page }) => {
  await mockReview(page, [ticket(101), ticket(102), flagged(103)]);
  await page.goto("/upload?batch=5&ticket=101");
  await expect(page.getByLabel("Ticket number")).toBeFocused();
  await page.keyboard.type("nfNF");
  await expect(page.getByLabel("Ticket number")).toHaveValue("nfNF");
  await expect(page.getByText("Ticket 1 of 3")).toBeVisible();
});

test("jumping still protects unsaved work", async ({ page }) => {
  await mockReview(page, [complete(101), complete(102)]);
  await page.goto("/upload?batch=5&ticket=101");
  await page.getByLabel("Tonnage (tons)").fill("25.000");
  await leaveField(page);
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.keyboard.press("n");
  await expect(page.getByText("Ticket 1 of 2")).toBeVisible();
  await expect(page.getByLabel("Tonnage (tons)")).toHaveValue("25.000");
});

test("the position is plain, and the current ticket stays in view in a long queue", async ({ page }) => {
  const tickets = Array.from({ length: 300 }, (_, i) => (i < 120 ? verified(101 + i) : complete(101 + i)));
  await mockReview(page, tickets);
  await page.goto("/upload?batch=5&ticket=351");
  await expect(page.getByTestId("position")).toContainText("Ticket 251 of 300");
  await expect(page.getByTestId("position")).toContainText("180 to review");
  await page.getByRole("button", { name: "All", exact: true }).click();
  const row = page.getByRole("region", { name: "Ticket queue" }).locator("[aria-current=true]");
  await expect(row).toContainText("#251");
  await expect(row).toBeInViewport();
  await leaveField(page);
  for (let i = 0; i < 25; i += 1) await page.keyboard.press("j");
  await expect(page.getByTestId("position")).toContainText("Ticket 276 of 300");
  await expect(row).toContainText("#276");
  await expect(row).toBeInViewport();
});

test("a screen reader hears which ticket is open", async ({ page }) => {
  await mockReview(page, [complete(101, { ticket_number: "10482" }), complete(102, { ticket_number: "10483" })]);
  await page.goto("/upload?batch=5&ticket=101");
  const announce = page.getByTestId("position-announcement");
  await expect(announce).toHaveAttribute("aria-live", "polite");
  await leaveField(page);
  await page.keyboard.press("j");
  await expect(announce).toHaveText("Showing 2 of 2: ticket number 10483, needs review.");
});

test("opening a batch again picks up where this person left off", async ({ page }) => {
  await mockReview(page, [complete(101), complete(102), complete(103), complete(104)]);
  await page.goto("/upload?batch=5&ticket=101");
  await leaveField(page);
  await page.keyboard.press("j");
  await page.keyboard.press("j");
  await expect(page.getByText("Ticket 3 of 4")).toBeVisible();
  await page.goto("/upload");
  await page.goto("/upload?batch=5");
  await expect(page.getByText("Ticket 3 of 4")).toBeVisible();
  await expect(page.getByTestId("resumed")).toContainText("Picked up where you left off");
  await page.getByRole("button", { name: "Go to the first ticket to review" }).click();
  await expect(page.getByText("Ticket 1 of 4")).toBeVisible();
  await expect(page.getByTestId("resumed")).toHaveCount(0);
});

test("if the ticket left off at has been dealt with since, it picks up at the next one to review", async ({ page }) => {
  const tickets = [complete(101), complete(102), complete(103), complete(104)];
  await mockReview(page, tickets);
  await page.goto("/upload?batch=5&ticket=102");
  await expect(page.getByText("Ticket 2 of 4")).toBeVisible();
  tickets[1].status = "verified";
  tickets[2].status = "verified";
  await page.goto("/upload?batch=5");
  await expect(page.getByText("Ticket 4 of 4")).toBeVisible();
});

test("someone else's place in a batch isn't where this person lands", async ({ page }) => {
  await page.addInitScript(() => window.localStorage.setItem("ticketflow.place.2.5", "103"));
  await mockReview(page, [complete(101), complete(102), complete(103)], { account: { id: 1 } });
  await page.goto("/upload?batch=5");
  await expect(page.getByText("Ticket 1 of 3")).toBeVisible();
  await expect(page.getByTestId("resumed")).toHaveCount(0);
});

test("the shortcut list includes the jump keys", async ({ page }) => {
  await mockReview(page, [complete(101)]);
  await page.goto("/upload?batch=5&ticket=101");
  await leaveField(page);
  await page.keyboard.press("?");
  const dialog = page.getByRole("dialog", { name: "Keyboard shortcuts" });
  await expect(dialog).toContainText("Next ticket that still needs review");
  await expect(dialog).toContainText("Next flagged ticket");
  await expect(dialog).not.toContainText(/accept all|approve all/i);
});
