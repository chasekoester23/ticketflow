// The ordinary Playwright test, with the browser's CPU slowed down for every page.
// Timing bugs that a fast machine hides tend to show up here. flake-hunt.sh makes the
// mocked browser specs import this instead of "@playwright/test". Pages opened from a
// context a test creates itself aren't throttled; everything using the page fixture is.
import { expect, test as base } from "@playwright/test";

const rate = Number(process.env.FLAKE_THROTTLE ?? "1");

export { expect };
export const test = base.extend({
  throttleBrowserCpu: [async ({ context }, use) => {
    const slow = async (page) => {
      const session = await context.newCDPSession(page);
      await session.send("Emulation.setCPUThrottlingRate", { rate });
    };
    if (rate > 1) context.on("page", (page) => { slow(page).catch(() => {}); });
    await use();
  }, { auto: true }],
});
