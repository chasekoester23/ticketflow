import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  timeout: 180_000,
  workers: 1,
  reporter: process.env.CI ? [["github"], ["line"]] : "line",
  use: { baseURL: process.env.DEMO_WEB_URL, trace: "retain-on-failure", ...devices["Desktop Chrome"] },
});
