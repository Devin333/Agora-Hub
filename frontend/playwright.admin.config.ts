import { defineConfig, devices } from "@playwright/test"
import { loadRootEnv } from "./root-env"

loadRootEnv(process.cwd())

// One frontend process serves one surface, so the Operations Studio specs (tagged @admin)
// run against their own admin dev server instead of the portal server of playwright.config.ts.
const baseURL = "http://127.0.0.1:3301"

export default defineConfig({
  testDir: "./tests/e2e",
  grep: /@admin/,
  fullyParallel: true,
  workers: process.env.PLAYWRIGHT_WORKERS ? Number(process.env.PLAYWRIGHT_WORKERS) : 1,
  reporter: "list",
  outputDir: "test-results/admin-surface",
  use: {
    baseURL,
    trace: "on-first-retry"
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] }
    }
  ],
  webServer: {
    command: "npm run dev:admin -- --hostname 127.0.0.1 --port 3301",
    url: baseURL,
    reuseExistingServer: process.env.PLAYWRIGHT_REUSE_EXISTING_SERVER === "true"
  }
})
