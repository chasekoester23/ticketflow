import { defineConfig, devices } from "@playwright/test";

const ci = Boolean(process.env.CI);
const port = process.env.TF_TEST_PORT ?? "3000";

export default defineConfig({
  testDir: "./browser",
  timeout: 30_000,
  workers: ci ? 1 : undefined,
  retries: ci ? 1 : 0,
  // The github reporter names failing tests in annotations anyone can read, so
  // a failure can be diagnosed without admin access to the run's logs.
  reporter: ci ? [["github"], ["line"], ["html", { open: "never", outputFolder: "playwright-report" }]] : "line",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: {
    // CI tests the production build: it's what people use, and pages don't pause
    // to compile on first visit or run effects twice as development mode does.
    command: ci ? `npx next build && npx next start -p ${port}` : `npx next dev -p ${port}`,
    cwd: new URL("..", import.meta.url).pathname,
    url: `http://127.0.0.1:${port}/upload`,
    reuseExistingServer: !ci,
    timeout: 240_000,
  },
});
