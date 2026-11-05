import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  timeout: 180_000,
  workers: 1,
  use: { baseURL: process.env.DEMO_WEB_URL, ...devices["Desktop Chrome"] },
});
