import { expect, test } from "@playwright/test";
import { profile } from "./accounts.mjs";
import { json, mockWorkspace } from "./f_helpers.mjs";

const nav = (page) => page.getByRole("navigation", { name: "Main navigation" });
const marked = (page) => page.evaluate(() => window.__sameDocument === true && document.querySelector(".tf-sidebar").__sameShell === true);
const mark = (page) => page.evaluate(() => { window.__sameDocument = true; document.querySelector(".tf-sidebar").__sameShell = true; });

test("the sidebar changes pages without reloading, remounting the shell, or re-asking who you are", async ({ page }) => {
  let asked = 0;
  page.on("request", (call) => { if (call.url().endsWith("/api/auth/me")) asked += 1; });
  await mockWorkspace(page);
  await page.goto("/");
  await expect(page.locator(".tf-page-title")).toHaveText("Delivery operations");
  await mark(page);
  for (const [link, path, title] of [["Delivery records", "/records", "Delivery records"], ["Projects", "/projects", "Projects"], ["Overview", "/", "Delivery operations"]]) {
    await nav(page).getByRole("link", { name: link, exact: true }).click();
    await expect(page).toHaveURL(path);
    await expect(page.locator(".tf-page-title")).toHaveText(title);
    await expect(nav(page).getByRole("link", { name: link, exact: true })).toHaveAttribute("aria-current", "page");
    await expect(nav(page).locator("[aria-current=page]")).toHaveCount(1);
    expect(await marked(page)).toBe(true);
  }
  expect(asked).toBe(1);
});

test("the new page is announced and focused the way a reload would have done", async ({ page }) => {
  await mockWorkspace(page);
  await page.goto("/");
  await expect(page.locator(".tf-page-title")).toBeVisible();
  await nav(page).getByRole("link", { name: "Projects", exact: true }).click();
  await expect(page).toHaveTitle("Projects · TicketFlow");
  await expect(page.locator("#main-content")).toBeFocused();
});

test("a link inside a page opens its filters without a reload", async ({ page }) => {
  await mockWorkspace(page);
  await page.goto("/");
  await expect(page.locator(".tf-page-title")).toBeVisible();
  await mark(page);
  await page.getByRole("link", { name: /Awaiting verification/ }).click();
  await expect(page).toHaveURL(/\/records\?status=needs_review/);
  await expect(page.getByLabel("Review status")).toHaveValue("needs_review");
  expect(await marked(page)).toBe(true);
});

test("the current page's sidebar item clears what's in the address", async ({ page }) => {
  await mockWorkspace(page);
  await page.goto("/records?status=needs_review");
  await expect(page.getByLabel("Review status")).toHaveValue("needs_review");
  await nav(page).getByRole("link", { name: "Delivery records", exact: true }).click();
  await expect(page).toHaveURL(/\/records$/);
  await expect(page.getByLabel("Review status")).toHaveValue("");
});

test("leaving a ticket with unsaved corrections asks first, and keeps them", async ({ page }) => {
  await mockWorkspace(page);
  await page.goto("/upload?batch=5&ticket=1001");
  const tons = page.getByLabel("Tonnage (tons)");
  await tons.fill("31.5");
  await expect.poll(() => page.evaluate(() => Object.keys(localStorage).filter((key) => key.startsWith("ticketflow.draft.")).length)).toBe(1);
  let asked = "";
  let answer = false;
  page.on("dialog", (dialog) => { asked = dialog.message(); return answer ? dialog.accept() : dialog.dismiss(); });
  const link = nav(page).getByRole("link", { name: "Projects", exact: true });
  await link.click();
  expect(asked).toContain("unsaved ticket corrections");
  await expect(page).toHaveURL(/\/upload\?batch=5/);
  await expect(tons).toHaveValue("31.5");
  answer = true;
  await link.click();
  await expect(page).toHaveURL("/projects");
  await page.goto("/upload?batch=5&ticket=1001");
  await expect(page.getByTestId("draft-restored")).toBeVisible();
  await expect(page.getByLabel("Tonnage (tons)")).toHaveValue("31.5");
});

test("a session that has ended is noticed on the next page change", async ({ page }) => {
  await page.addInitScript(() => { const real = Date.now; window.__skew = 0; Date.now = () => real() + window.__skew; });
  await mockWorkspace(page);
  await page.goto("/");
  await expect(page.locator(".tf-page-title")).toBeVisible();
  await page.route("**/api/auth/me", (route) => json(route, { detail: "Sign in" }, 401));
  await page.evaluate(() => { window.__skew = 31_000; });
  await nav(page).getByRole("link", { name: "Projects", exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
});

test("a server hiccup during that check doesn't sign anyone out", async ({ page }) => {
  await page.addInitScript(() => { const real = Date.now; window.__skew = 0; Date.now = () => real() + window.__skew; });
  await mockWorkspace(page);
  await page.goto("/");
  await expect(page.locator(".tf-page-title")).toBeVisible();
  await page.route("**/api/auth/me", (route) => json(route, { detail: "Busy" }, 503));
  await page.evaluate(() => { window.__skew = 31_000; });
  await nav(page).getByRole("link", { name: "Projects", exact: true }).click();
  await expect(page.locator(".tf-page-title")).toHaveText("Projects");
  await expect(page).toHaveURL("/projects");
});

test("a role taken away shows up on the next check, and the link leaves the menu", async ({ page }) => {
  await page.addInitScript(() => { const real = Date.now; window.__skew = 0; Date.now = () => real() + window.__skew; });
  await mockWorkspace(page);
  await page.goto("/");
  await expect(nav(page).getByRole("link", { name: "Team", exact: true })).toBeVisible();
  await page.route("**/api/auth/me", (route) => json(route, profile("viewer")));
  await page.evaluate(() => { window.__skew = 31_000; });
  await nav(page).getByRole("link", { name: "Projects", exact: true }).click();
  await expect(nav(page).getByRole("link", { name: "Team", exact: true })).toHaveCount(0);
});
