import { defineConfig, devices } from "@playwright/test";

const managedServerCommand = process.env.PLAYWRIGHT_SERVER_MODE === "production"
  ? "cp -R .next/static .next/standalone/.next && cp -R public .next/standalone && PORT=13000 HOSTNAME=127.0.0.1 node .next/standalone/server.js"
  : "npm run dev -- --port 13000";

export default defineConfig({
  testDir: "./tests/e2e",
  outputDir: "/tmp/auto-gallery-playwright/results",
  fullyParallel: true,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000",
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
      : undefined,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: process.env.PLAYWRIGHT_MANAGE_SERVER
    ? {
        command: managedServerCommand,
        url: "http://127.0.0.1:13000",
        reuseExistingServer: false,
        timeout: 120_000,
      }
    : undefined,
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
