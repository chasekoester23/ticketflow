import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./browser",
  timeout: 30_000,
  workers: process.env.CI ? 1 : undefined,
  retries: process.env.CI ? 1 : 0,
  use: {
    baseURL: "http://127.0.0.1:3000",
    ...devices["Desktop Chrome"],
  },
  webServer: {
    command: "npm run dev",
    url: "http://127.0.0.1:3000/upload",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
