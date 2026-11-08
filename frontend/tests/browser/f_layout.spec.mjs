import { expect, test } from "@playwright/test";
import { mockWorkspace } from "./f_helpers.mjs";

const PAGES = ["/login", "/", "/upload", "/upload?batch=5&ticket=1001", "/records", "/records/names", "/invoices", "/invoices?line=9",
  "/invoices/documents", "/reports/project?project_id=1", "/reports", "/projects", "/pricing", "/team", "/account", "/system"];
const SIZES = [[390, 844, "phone"], [1366, 768, "laptop"]];

for (const [width, height, size] of SIZES) {
  for (const path of PAGES) {
    test(`${path} has no sideways scroll on a ${width}px ${size}`, async ({ page }) => {
      await page.setViewportSize({ width, height });
      await mockWorkspace(page);
      await page.goto(path);
      await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
      await page.waitForTimeout(300);
      const { scroll, client } = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
      expect(scroll, `${path} is ${scroll}px wide in a ${client}px window`).toBeLessThanOrEqual(client);
    });
  }
}

/** True when nothing sits on top of the element at its centre, so a tap lands on it. */
const uncovered = (locator) => locator.evaluate((element) => {
  const box = element.getBoundingClientRect();
  const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
  return element === hit || element.contains(hit);
});

test("on a phone the scan comes before the queue, and Accept stays reachable while checking fields", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockWorkspace(page);
  await page.goto("/upload?batch=5&ticket=1001");
  const accept = page.getByRole("button", { name: "Accept & next" });
  await expect(accept).toBeVisible();
  // The queue starts tucked away, one tap from the scan's header.
  await expect(page.getByRole("region", { name: "Ticket queue" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Show queue" })).toBeVisible();
  const scan = page.getByTestId("scan-box");
  const scanBox = await scan.boundingBox();
  expect(scanBox.y + scanBox.height).toBeLessThan(844 * 1.5);
  // While someone works down the fields, Accept is on screen and nothing covers it.
  for (const label of ["Ticket number", "Tonnage (tons)", "Supplier"]) {
    await page.getByLabel(label).scrollIntoViewIfNeeded();
    await expect(accept).toBeInViewport();
    expect(await uncovered(accept)).toBe(true);
    expect(await uncovered(page.getByLabel(label))).toBe(true);
  }
  await page.getByRole("button", { name: "Show queue" }).click();
  await expect(page.getByRole("region", { name: "Ticket queue" })).toBeVisible();
});

test("a swipe that starts on the scan still scrolls a phone's page, until the scan is zoomed", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockWorkspace(page);
  await page.goto("/upload?batch=5&ticket=1001");
  const scan = page.getByTestId("scan-box");
  await expect(scan).toBeVisible();
  expect(await scan.evaluate((element) => getComputedStyle(element).touchAction)).toBe("pan-y");
  await page.getByRole("button", { name: "Zoom in" }).click();
  expect(await scan.evaluate((element) => getComputedStyle(element).touchAction)).toBe("none");
});

test("on a 1366 x 768 laptop the scan and Accept are on screen together without scrolling", async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  await mockWorkspace(page);
  await page.goto("/upload?batch=5&ticket=1001");
  const accept = page.getByRole("button", { name: "Accept & next" });
  await expect(accept).toBeVisible();
  await expect.poll(async () => { const box = await accept.boundingBox(); return box.y + box.height; }).toBeLessThanOrEqual(768);
  const scan = await page.getByTestId("scan-box").boundingBox();
  expect(scan.height).toBeGreaterThan(250);
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
  expect(await uncovered(accept)).toBe(true);
});
