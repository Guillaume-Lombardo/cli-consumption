import { defineConfig, devices } from "@playwright/test";

const dashboardOrigin = "http://127.0.0.1:4310";

export default defineConfig({
  expect: { timeout: 10_000 },
  fullyParallel: false,
  projects: [
    {
      name: "chromium-desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 1000 } },
    },
    {
      name: "chromium-tablet",
      use: { ...devices["Desktop Chrome"], viewport: { width: 768, height: 1024 } },
    },
    { name: "chromium-mobile", use: { ...devices["Pixel 7"] } },
  ],
  reporter: "line",
  retries: process.env.CI ? 1 : 0,
  testDir: "./e2e",
  use: {
    baseURL: dashboardOrigin,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: "node e2e/mock-collector.mjs",
      port: 4311,
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
    {
      command: "npm run start -- --hostname 127.0.0.1 --port 4310",
      env: {
        CLI_CONSUMPTION_API_URL: "http://127.0.0.1:4311",
        CLI_CONSUMPTION_DASHBOARD_ORIGIN: dashboardOrigin,
        CLI_CONSUMPTION_DASHBOARD_PASSWORD: "e2e dashboard password",
        CLI_CONSUMPTION_EXPORT_TOKEN: "e2e-export-token",
        CLI_CONSUMPTION_READ_TOKEN: "e2e-read-token",
        CLI_CONSUMPTION_LAYOUT_TOKEN: "e2e-layout-token",
        CLI_CONSUMPTION_SESSION_SECRET:
          "e2e-session-secret-with-at-least-thirty-two-bytes",
      },
      port: 4310,
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
  ],
  workers: 1,
});
