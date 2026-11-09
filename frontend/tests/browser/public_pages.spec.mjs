import { expect, test } from "@playwright/test";
import { profile } from "./accounts.mjs";
import { cors, json } from "./f_helpers.mjs";

async function mockPublic(page, { me = null, needed = false, login, request } = {}) {
  const log = { requests: [], headers: [], setups: 0 };
  await page.route("**/api/**", async (route) => {
    const call = route.request();
    const path = new URL(call.url()).pathname;
    if (call.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (path === "/api/auth/me") return me ? json(route, me) : json(route, { detail: "Sign in" }, 401);
    if (path === "/api/setup/status") return json(route, { needed });
    if (path === "/api/auth/login") return login ? login(route) : json(route, profile("owner"));
    if (path === "/api/access-requests") {
      log.requests.push(call.postDataJSON());
      log.headers.push(call.headers()["x-ticketflow-request"]);
      return request ? request(route) : json(route, { received: true }, 202);
    }
    if (path === "/api/setup") { log.setups += 1; return json(route, { detail: "no" }, 500); }
    return json(route, { detail: "Not Found" }, 404);
  });
  return log;
}

const noSideScroll = (page) => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);

test("a first-time visitor to the app lands on the welcome page, with no sign-in fields", async ({ page }) => {
  await mockPublic(page);
  await page.goto("/");
  await expect(page).toHaveURL(/\/welcome$/);
  await expect(page.getByRole("heading", { level: 1 })).toContainText("before you pay for it");
  await expect(page.locator("input")).toHaveCount(0);
});

test("the welcome page leads to sign-in and to create account, and each leads back", async ({ page }) => {
  await mockPublic(page);
  await page.goto("/welcome");
  await page.getByRole("main").getByRole("link", { name: "Sign in" }).first().click();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Sign in to TicketFlow" })).toBeVisible();
  await page.getByRole("link", { name: "Back to home" }).click();
  await expect(page).toHaveURL(/\/welcome$/);
  await page.getByRole("main").getByRole("link", { name: "Request access" }).first().click();
  await expect(page).toHaveURL(/\/signup$/);
  await expect(page.getByRole("form", { name: "Request access" })).toBeVisible();
});

for (const path of ["/welcome", "/login", "/signup"]) {
  test(`someone already signed in is sent from ${path} to their workspace`, async ({ page }) => {
    await mockPublic(page, { me: profile("owner") });
    await page.goto(path);
    await expect(page).toHaveURL(/127\.0\.0\.1:\d+\/$/);
  });

  for (const [width, height] of [[1440, 900], [820, 1100], [390, 844]]) {
    test(`${path} has no sideways scroll at ${width}px`, async ({ page }) => {
      await mockPublic(page);
      await page.setViewportSize({ width, height });
      await page.goto(path);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      expect(await noSideScroll(page)).toBe(true);
    });
  }
}

test("every link on the site goes somewhere", async ({ page, request }) => {
  await mockPublic(page);
  await page.goto("/welcome");
  const hrefs = await page.locator("a[href]").evaluateAll((links) => [...new Set(links.map((link) => link.getAttribute("href")))]);
  expect(hrefs).toEqual(expect.arrayContaining(["/login", "/signup", "/welcome", "#tickets", "#invoices"]));
  for (const href of hrefs) {
    if (href.startsWith("#")) await expect(page.locator(href)).toHaveCount(1);
    else expect((await request.get(href)).status(), href).toBe(200);
  }
});

test("the site says signing up is gated, labels its examples, and loads nothing from other companies", async ({ page }) => {
  const outside = [];
  page.on("request", (call) => { const host = new URL(call.url()).hostname; if (!["127.0.0.1", "localhost"].includes(host)) outside.push(call.url()); });
  await mockPublic(page);
  await page.goto("/welcome");
  await page.evaluate(() => document.fonts.ready);
  await expect(page.getByRole("main").getByText("You can't sign yourself up yet.")).toBeVisible();
  await expect(page.getByRole("contentinfo").getByText("fictional examples")).toBeVisible();
  await expect(page.getByRole("img", { name: /review screen/ })).toBeVisible();
  expect(await page.locator("img").evaluate((image) => image.naturalWidth)).toBeGreaterThan(0);
  expect(await page.evaluate(() => document.fonts.check('600 40px "Barlow Semi Condensed"'))).toBe(true);
  expect(outside).toEqual([]);
});

test("the site can be walked from the keyboard, with a visible focus ring", async ({ page }) => {
  await mockPublic(page);
  await page.goto("/welcome");
  const seen = [];
  for (let press = 0; press < 7; press += 1) {
    await page.keyboard.press("Tab");
    seen.push(await page.evaluate(() => ({ text: document.activeElement.textContent.trim(), outline: getComputedStyle(document.activeElement).outlineStyle })));
  }
  expect(seen.map((item) => item.text)).toEqual(expect.arrayContaining(["Tickets", "Invoices", "Sign in", "Request access"]));
  expect(seen.every((item) => item.outline !== "none")).toBe(true);
});

test("a redirect target in the address is ignored after sign-in", async ({ page }) => {
  await mockPublic(page);
  await page.goto("/login?next=https://evil.example/&redirect=//evil.example");
  await page.getByLabel("Username").fill("owner");
  await page.getByLabel("Password", { exact: true }).fill("correct-long-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.waitForURL((url) => url.pathname === "/");
  expect(new URL(page.url()).hostname).toBe("127.0.0.1");
});

test("sign-in works for a password manager and can show the password", async ({ page }) => {
  await mockPublic(page);
  await page.goto("/login");
  const password = page.getByLabel("Password", { exact: true });
  await expect(page.getByLabel("Username")).toHaveAttribute("autocomplete", "username");
  await expect(password).toHaveAttribute("autocomplete", "current-password");
  await expect(password).toHaveAttribute("type", "password");
  await password.fill("a fictional password");
  await page.getByRole("button", { name: "Show password" }).click();
  await expect(password).toHaveAttribute("type", "text");
  await expect(password).toHaveValue("a fictional password");
  await page.getByRole("button", { name: "Hide password" }).click();
  await expect(password).toHaveAttribute("type", "password");
});

test("an empty sign-in says what's missing and sends nothing", async ({ page }) => {
  let sent = 0;
  await mockPublic(page, { login: (route) => { sent += 1; return json(route, profile("owner")); } });
  await page.goto("/login");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByText("Enter your username.")).toBeVisible();
  await expect(page.getByText("Enter your password.")).toBeVisible();
  await expect(page.getByLabel("Username")).toBeFocused();
  expect(sent).toBe(0);
});

test("a failed sign-in shows the server's reason, and the button comes back", async ({ page }) => {
  await mockPublic(page, { login: (route) => json(route, { detail: "Invalid credentials" }, 401) });
  await page.goto("/login");
  await page.getByLabel("Username").fill("owner");
  await page.getByLabel("Password", { exact: true }).fill("wrong-long-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.locator(".tf-pub-banner-error")).toContainText("Invalid credentials");
  await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeEnabled();
  await expect(page.getByLabel("Password", { exact: true })).toBeFocused();
});

test("signing in is disabled while it's in progress and can't be sent twice", async ({ page }) => {
  let sent = 0;
  let finish;
  const held = new Promise((resolve) => { finish = resolve; });
  await mockPublic(page, { login: async (route) => { sent += 1; await held; return json(route, { detail: "Invalid credentials" }, 401); } });
  await page.goto("/login");
  await page.getByLabel("Username").fill("owner");
  await page.getByLabel("Password", { exact: true }).fill("a-long-fictional-password");
  await page.getByLabel("Password", { exact: true }).press("Enter");
  await expect(page.getByRole("button", { name: "Signing in…" })).toBeDisabled();
  await page.getByLabel("Password", { exact: true }).press("Enter");
  finish();
  await expect(page.locator(".tf-pub-banner-error")).toBeVisible();
  expect(sent).toBe(1);
});

test("sign-in can be completed from the keyboard alone", async ({ page }) => {
  await mockPublic(page);
  await page.goto("/login");
  await page.getByLabel("Username").focus();
  await page.keyboard.type("owner");
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("Password", { exact: true })).toBeFocused();
  await page.keyboard.type("correct-long-password");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Show password" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeFocused();
  await page.keyboard.press("Enter");
  await page.waitForURL((url) => url.pathname === "/");
});

test("a server with no accounts sends sign-in to first-time setup", async ({ page }) => {
  await mockPublic(page, { needed: true });
  await page.goto("/login");
  await expect(page).toHaveURL(/\/signup$/);
  await expect(page.getByRole("heading", { name: "Set up TicketFlow" })).toBeVisible();
});

test("a request for access says plainly that no account exists yet", async ({ page }) => {
  const log = await mockPublic(page);
  await page.goto("/signup");
  const form = page.getByRole("form", { name: "Request access" });
  await form.getByLabel("Company name").fill("Fictional Paving Co");
  await form.getByLabel("Your name").fill("Fictional Person");
  await form.getByLabel("Work email").fill("person@fictional.example");
  await form.getByLabel(/tickets per month/).selectOption("500_2000");
  await form.getByRole("button", { name: "Request access" }).click();
  await expect(page.getByRole("heading", { name: "Request received" })).toBeFocused();
  await expect(page.getByText("No account has been created yet.")).toBeVisible();
  expect(log.setups).toBe(0);
  expect(log.headers).toEqual(["1"]);
  expect(log.requests).toEqual([{ company_name: "Fictional Paving Co", contact_name: "Fictional Person", email: "person@fictional.example", monthly_tickets: "500_2000", website: "" }]);
});

test("the request form checks its fields before sending anything", async ({ page }) => {
  const log = await mockPublic(page);
  await page.goto("/signup");
  const form = page.getByRole("form", { name: "Request access" });
  await form.getByRole("button", { name: "Request access" }).click();
  await expect(form.getByText("Enter your company's name.")).toBeVisible();
  await expect(form.getByLabel("Company name")).toBeFocused();
  await form.getByLabel("Company name").fill("Fictional Paving Co");
  await form.getByLabel("Your name").fill("Fictional Person");
  await form.getByLabel("Work email").fill("not-an-address");
  await form.getByRole("button", { name: "Request access" }).click();
  await expect(form.getByText("Enter an email address like")).toBeVisible();
  await expect(form.getByLabel("Work email")).toHaveAttribute("aria-invalid", "true");
  expect(log.requests).toEqual([]);
});

test("a refused request keeps what was typed and says why", async ({ page }) => {
  await mockPublic(page, { request: (route) => json(route, { detail: "Too many requests from this network. Try again in 40 seconds." }, 429) });
  await page.goto("/signup");
  const form = page.getByRole("form", { name: "Request access" });
  await form.getByLabel("Company name").fill("Fictional Paving Co");
  await form.getByLabel("Your name").fill("Fictional Person");
  await form.getByLabel("Work email").fill("person@fictional.example");
  await form.getByRole("button", { name: "Request access" }).click();
  await expect(page.locator(".tf-pub-banner-error")).toContainText("Too many requests");
  await expect(form.getByLabel("Company name")).toHaveValue("Fictional Paving Co");
  await expect(page.getByRole("heading", { name: "Request received" })).toHaveCount(0);
});

test("the phone menu shows who's signed in, and Escape closes it", async ({ page }) => {
  await mockPublic(page, { me: profile("clerk", { display_name: "Fictional Clerk", organization_name: "Fictional Paving Co" }) });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/account");
  await expect(page.locator(".tf-organization-name")).toHaveText("Fictional Paving Co");
  const toggle = page.getByRole("button", { name: "Open navigation" });
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("link", { name: /Your account/ })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
});

test("the public pages have no automatically detectable accessibility problems", async ({ page }) => {
  let AxeBuilder;
  try { ({ default: AxeBuilder } = await import("@axe-core/playwright")); } catch { test.skip(true, "axe isn't installed here; CI installs it"); return; }
  await mockPublic(page);
  for (const width of [390, 1366]) {
    await page.setViewportSize({ width, height: 900 });
    for (const path of ["/welcome", "/login", "/signup"]) {
      await page.goto(path);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      const { violations } = await new AxeBuilder({ page }).analyze();
      expect(violations.map((v) => `${path}@${width}: ${v.id}`)).toEqual([]);
    }
  }
});
