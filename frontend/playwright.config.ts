import path from "node:path"
import { defineConfig, devices } from "@playwright/test"
import { loadRootEnv } from "./root-env"

const configDir = process.cwd()

loadRootEnv(configDir)

const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:3000"
const webServerCommand = process.env.PLAYWRIGHT_WEB_SERVER_COMMAND ?? "npm run dev"

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  workers: process.env.PLAYWRIGHT_WORKERS ? Number(process.env.PLAYWRIGHT_WORKERS) : 1,
  reporter: "list",
  // Operations Studio specs need the admin surface; playwright.admin.config.ts runs them.
  grepInvert: /@admin/,
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
    command: webServerCommand,
    url: baseURL,
    reuseExistingServer: process.env.PLAYWRIGHT_REUSE_EXISTING_SERVER === "true"
  }
})
