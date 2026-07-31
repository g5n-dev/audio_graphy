import { defineConfig, devices } from "@playwright/test";

const e2ePort = process.env.E2E_PORT ?? "4176";
const baseURL =
  process.env.E2E_BASE_URL ?? `http://127.0.0.1:${e2ePort}`;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: [
    ["line"],
    ["html", { outputFolder: "playwright-report", open: "never" }],
  ],
  use: {
    baseURL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    actionTimeout: 10_000,
    navigationTimeout: 30_000,
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1600, height: 1000 },
      },
    },
  ],
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: `npm run dev -- --mode sites --host 127.0.0.1 --port ${e2ePort} --strictPort`,
        url: baseURL,
        // Reusing an arbitrary Vite server can silently test a different
        // worktree or mode. Every local run owns an isolated demo worker.
        reuseExistingServer: false,
        timeout: 120_000,
      },
});
