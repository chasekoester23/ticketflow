import { expect, test } from "@playwright/test";
import { complete, mockReview, ticket } from "./f_helpers.mjs";

const REVIEW = "/upload?batch=5&ticket=101";
const dayFromToday = (days) => {
  const date = new Date();
  date.setDate(date.getDate() + days);
  return [date.getFullYear(), String(date.getMonth() + 1).padStart(2, "0"), String(date.getDate()).padStart(2, "0")].join("-");
};

test("tons above the plausible limit warn about pounds, convert nothing, and don't block", async ({ page }) => {
  const log = await mockReview(page, [complete(101), complete(102)]);
  await page.goto(REVIEW);
  const tons = page.getByLabel("Tonnage (tons)");
  await tons.fill("48000");
  const said = page.getByTestId("message-tonnage");
  await expect(said).toContainText("above 60 tons");
  await expect(said).toContainText("pounds rather than tons");
  await expect(said).toContainText("doesn't convert units");
  await expect(tons).toHaveValue("48000");
  await expect(tons).not.toHaveAttribute("aria-invalid", "true");
  await expect(tons).toHaveAttribute("aria-describedby", /message-tonnage/);
  await page.getByRole("button", { name: "Accept & next" }).click();
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  expect(log.patched[0].body).toEqual({ tonnage: "48000" });
  expect(log.verified).toEqual([101]);
});

for (const [value, warns] of [["60", false], ["60.000", false], ["059.999", false], ["60.001", true], ["61", true], ["100", true]]) {
  test(`tons of ${value} ${warns ? "warn" : "don't warn"}`, async ({ page }) => {
    await mockReview(page, [complete(101)]);
    await page.goto(REVIEW);
    await page.getByLabel("Tonnage (tons)").fill(value);
    await expect(page.getByTestId("message-tonnage")).toHaveCount(warns ? 1 : 0);
  });
}

for (const value of ["48000 lbs", "1,250", "-3", ".", "2.1234", "12t"]) {
  test(`tons of "${value}" can't be saved, and the message says what to enter`, async ({ page }) => {
    const log = await mockReview(page, [complete(101)]);
    await page.goto(REVIEW);
    const tons = page.getByLabel("Tonnage (tons)");
    await tons.fill(value);
    await expect(page.getByTestId("message-tonnage")).toContainText("Enter tons as a number");
    await expect(tons).toHaveAttribute("aria-invalid", "true");
    await expect(page.getByRole("button", { name: "Accept & next" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(log.patched).toEqual([]);
  });
}

test("zero tons says why, and accepting goes back to the field instead of sending it", async ({ page }) => {
  const log = await mockReview(page, [complete(101)]);
  await page.goto(REVIEW);
  const tons = page.getByLabel("Tonnage (tons)");
  await tons.fill("0.000");
  await expect(page.getByTestId("message-tonnage")).toContainText("must be more than zero");
  await page.getByLabel("Supplier").focus();
  await page.getByRole("button", { name: "Accept & next" }).click();
  await expect(tons).toBeFocused();
  await page.waitForTimeout(100);
  expect(log.requests.filter((r) => r.startsWith("PATCH") || r.endsWith("/verify"))).toEqual([]);
});

test("a future date warns but doesn't block: the server keeps the final word", async ({ page }) => {
  const log = await mockReview(page, [complete(101), complete(102)]);
  await page.goto(REVIEW);
  const date = page.getByLabel("Delivery date");
  await date.fill(dayFromToday(3));
  const said = page.getByTestId("message-delivery_date");
  await expect(said).toContainText("in the future");
  await expect(said).toContainText(dayFromToday(0));
  await expect(date).not.toHaveAttribute("aria-invalid", "true");
  await expect(page.getByRole("button", { name: "Accept & next" })).toBeEnabled();
  await page.getByRole("button", { name: "Accept & next" }).click();
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  expect(log.patched[0].body).toEqual({ delivery_date: dayFromToday(3) });
  expect(log.verified).toEqual([101]);
});

test("a date years ago warns but doesn't block; today and last month are quiet", async ({ page }) => {
  const log = await mockReview(page, [complete(101), complete(102)]);
  await page.goto(REVIEW);
  const date = page.getByLabel("Delivery date");
  for (const quiet of [dayFromToday(0), dayFromToday(-30), dayFromToday(-700)]) {
    await date.fill(quiet);
    await expect(page.getByTestId("message-delivery_date")).toHaveCount(0);
  }
  await date.fill("2016-10-15");
  await expect(page.getByTestId("message-delivery_date")).toContainText("more than 2 years ago");
  await page.getByRole("button", { name: "Accept & next" }).click();
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  expect(log.verified).toEqual([101]);
});

test("a required field left blank says so when the cursor leaves it, and clears when filled", async ({ page }) => {
  await mockReview(page, [ticket(101)]);
  await page.goto(REVIEW);
  const number = page.getByLabel("Ticket number");
  await expect(number).toBeFocused();
  // Nothing shouts on a fresh ticket, but what's still needed is on view.
  await expect(page.getByTestId("message-ticket_number")).toHaveCount(0);
  await expect(page.getByTestId("accept-needs")).toContainText("ticket number, delivery date, material, tons, supplier, job location");
  await page.keyboard.press("Tab");
  await expect(page.getByTestId("message-ticket_number")).toContainText("Ticket number is needed to accept");
  await expect(number).toHaveAttribute("aria-invalid", "true");
  await expect(number).toHaveAttribute("aria-describedby", "message-ticket_number");
  await number.fill("10482");
  await expect(page.getByTestId("message-ticket_number")).toHaveCount(0);
  await expect(number).not.toHaveAttribute("aria-invalid", "true");
  await expect(page.getByTestId("accept-needs")).not.toContainText("ticket number");
});

test("a message appearing doesn't move the form out from under a click in progress", async ({ page }) => {
  await mockReview(page, [complete(101, { delivery_date: "2026-10-15" }), ticket(102)]);
  await page.goto("/upload?batch=5&ticket=102");
  await expect(page.getByLabel("Ticket number")).toBeFocused();
  // Pressing a button below the blank field blurs it. The message waits until the press is over.
  const material = page.getByLabel("Material");
  const box = await material.boundingBox();
  await page.mouse.move(box.x + 10, box.y + box.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(50);
  await expect(page.getByTestId("message-ticket_number")).toHaveCount(0);
  expect((await material.boundingBox()).y).toBe(box.y);
  await page.mouse.up();
  await expect(page.getByTestId("message-ticket_number")).toBeVisible();
  await expect(material).toBeFocused();
});

test("accepting with blanks sends nothing, shows every message, and lands on the first blank", async ({ page }) => {
  const log = await mockReview(page, [ticket(101, { ticket_number: "1", tonnage: "20.000" })]);
  await page.goto(REVIEW);
  await page.getByLabel("Ticket number").focus();
  await page.keyboard.press("Control+Enter");
  await expect(page.getByLabel("Delivery date")).toBeFocused();
  for (const field of ["delivery_date", "material_type", "supplier", "job_location"]) {
    await expect(page.getByTestId("message-" + field)).toBeVisible();
  }
  await page.waitForTimeout(100);
  expect(log.requests.filter((r) => r.startsWith("PATCH") || r.endsWith("/verify"))).toEqual([]);
});

test("the keyboard flow is intact: messages never take the cursor", async ({ page }) => {
  const log = await mockReview(page, [ticket(101), ticket(102)]);
  await page.goto(REVIEW);
  await expect(page.getByLabel("Ticket number")).toBeFocused();
  await page.keyboard.type("010482");
  await page.keyboard.press("Enter");
  await page.getByLabel("Delivery date").fill(dayFromToday(5));
  await page.keyboard.press("Enter");
  await expect(page.getByLabel("Material")).toBeFocused();
  await page.keyboard.type("No. 53 Stone");
  await page.keyboard.press("Enter");
  await page.keyboard.type("24000");
  await expect(page.getByLabel("Tonnage (tons)")).toBeFocused();
  await expect(page.getByTestId("message-tonnage")).toBeVisible();
  await page.keyboard.press("Enter");
  await page.keyboard.type("Example Aggregates");
  await page.keyboard.press("Enter");
  await page.keyboard.type("Lot A");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "Accept & next" })).toBeFocused();
  await page.keyboard.press("Control+Enter");
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  expect(log.verified).toEqual([101]);
});

test("a clerk can save blanks for later, and isn't told about accepting", async ({ page }) => {
  const log = await mockReview(page, [ticket(101), ticket(102)], { role: "clerk" });
  await page.goto(REVIEW);
  await page.getByLabel("Ticket number").fill("10482");
  await expect(page.getByTestId("accept-needs")).toHaveCount(0);
  await page.getByRole("button", { name: "Save & next" }).click();
  await expect(page.getByText("Ticket 2 of 2")).toBeVisible();
  expect(log.patched[0].body).toEqual({ ticket_number: "10482" });
  expect(log.verified).toEqual([]);
});

test("a viewer sees no advice on values they can't change", async ({ page }) => {
  await mockReview(page, [complete(101, { tonnage: "90.000" })], { role: "viewer" });
  await page.goto(REVIEW);
  await expect(page.getByLabel("Tonnage (tons)")).toHaveValue("90.000");
  await expect(page.getByTestId("message-tonnage")).toHaveCount(0);
});

test("a bad override rate is caught before saving", async ({ page }) => {
  const log = await mockReview(page, [complete(101)]);
  await page.goto(REVIEW);
  const rate = page.getByLabel("Override rate for this ticket ($/ton, optional)");
  await rate.fill("$31.25/ton");
  await expect(page.getByText("Enter the rate as dollars per ton")).toBeVisible();
  await expect(rate).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByRole("button", { name: "Save changes" })).toBeDisabled();
  await rate.fill("31.25");
  await expect(page.getByText("Enter the rate as dollars per ton")).toHaveCount(0);
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect.poll(() => log.patched.length).toBe(1);
  expect(log.patched[0].body).toEqual({ unit_price_override: "31.25" });
});
