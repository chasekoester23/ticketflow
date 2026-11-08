import { expect, test } from "@playwright/test";
import { mockWorkspace } from "./f_helpers.mjs";

// axe runs when @axe-core/playwright is installed. CI's install line doesn't add it
// yet, so there these checks skip rather than fail. The focus checks below always run.
let AxeBuilder = null;
try {
  ({ default: AxeBuilder } = await import("@axe-core/playwright"));
} catch {
  AxeBuilder = null;
}

const heading = (name) => (page) => page.getByRole("heading", { name, exact: true }).first();
const PAGES = [
  ["/login", heading("Sign in to TicketFlow")],
  ["/", heading("Delivery operations")],
  ["/upload", heading("Ticket intake & review")],
  ["/upload?batch=5&ticket=1001", (page) => page.getByRole("button", { name: "Accept & next" })],
  ["/records", heading("Delivery records")],
  ["/records/names", (page) => page.getByRole("heading", { level: 1 })],
  ["/invoices", heading("Invoice comparison")],
  ["/invoices/documents", heading("Invoice PDFs")],
  ["/reports/project", heading("Project report")],
  ["/reports", (page) => page.getByRole("heading", { level: 1 })],
  ["/projects", heading("Projects")],
  ["/pricing", heading("Suppliers & rates")],
  ["/team", (page) => page.getByRole("heading", { level: 1 })],
  ["/account", (page) => page.getByRole("heading", { level: 1 })],
  ["/system", (page) => page.getByRole("heading", { level: 1 })],
];
const STATES = [
  ["the shortcut list", "/upload?batch=5&ticket=1001", async (page) => {
    await page.getByRole("button", { name: "Keyboard shortcuts" }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
  }],
  ["an invoice line's details", "/invoices", async (page) => {
    await page.getByRole("button", { name: "Details" }).first().click();
    await expect(page.getByRole("region", { name: "Why this result" })).toBeVisible();
  }],
  ["the export columns dialog", "/records", async (page) => {
    await page.getByRole("button", { name: "Choose columns" }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
  }],
];

const describe = (violation) => `${violation.id} (${violation.impact}): ${violation.help} — ${violation.nodes.slice(0, 3).map((node) => node.target.join(" ")).join(" | ")}`;
async function axe(page) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
  return results.violations.map(describe);
}

for (const [width, height, size] of [[1366, 768, "laptop"], [390, 844, "phone"]]) {
  for (const [path, ready] of PAGES) {
    test(`axe finds nothing on ${path} at ${size} width`, async ({ page }) => {
      test.skip(!AxeBuilder, "@axe-core/playwright isn't installed here");
      await page.setViewportSize({ width, height });
      await mockWorkspace(page);
      await page.goto(path);
      await expect(ready(page)).toBeVisible();
      await page.waitForTimeout(300);
      expect(await axe(page)).toEqual([]);
    });
  }
  for (const [name, path, open] of STATES) {
    test(`axe finds nothing with ${name} open at ${size} width`, async ({ page }) => {
      test.skip(!AxeBuilder, "@axe-core/playwright isn't installed here");
      await page.setViewportSize({ width, height });
      await mockWorkspace(page);
      await page.goto(path);
      await open(page);
      await page.waitForTimeout(300);
      expect(await axe(page)).toEqual([]);
    });
  }
}

test("the shortcut list takes focus, keeps it, and gives it back on close", async ({ page }) => {
  await mockWorkspace(page);
  await page.goto("/upload?batch=5&ticket=1002");
  const opener = page.getByRole("button", { name: "Keyboard shortcuts" });
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "Keyboard shortcuts" });
  await expect(dialog.getByRole("button", { name: "Close" })).toBeFocused();
  for (let i = 0; i < 3; i += 1) {
    await page.keyboard.press("Tab");
    expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true);
  }
  await page.keyboard.press("Shift+Tab");
  expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeFocused();
});

for (const [name, opener, dialogName] of [["export columns", "Choose columns", "Export columns"], ["save view", "Save this view", "Save this view"]]) {
  test(`the ${name} dialog closes with Escape and gives focus back`, async ({ page }) => {
    await mockWorkspace(page);
    await page.goto("/records");
    const button = page.getByRole("button", { name: opener, exact: true });
    await button.click();
    const dialog = page.getByRole("dialog", { name: dialogName });
    await expect(dialog).toBeVisible();
    expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true);
    for (let i = 0; i < 25; i += 1) {
      await page.keyboard.press("Tab");
      expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true);
    }
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(button).toBeFocused();
  });
}

test("a keyboard user can skip the navigation straight to the page", async ({ page }) => {
  await mockWorkspace(page);
  await page.goto("/records");
  await expect(page.getByRole("heading", { name: "Delivery records", exact: true })).toBeVisible();
  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: "Skip to main content" });
  await expect(skip).toBeFocused();
  await expect(skip).toBeInViewport();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Tab");
  expect(await page.evaluate(() => document.getElementById("main-content")?.contains(document.activeElement))).toBe(true);
});
