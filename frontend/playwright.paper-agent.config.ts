import path from "node:path"
import { defineConfig, devices } from "@playwright/test"

const configDir = process.cwd()
// The hosted profile over plain HTTP on loopback; production requires HTTPS.
const publicOrigin = "http://127.0.0.1:3317"
const apiOrigin = "http://127.0.0.1:8317"

const hostedEnvironment = {
  NEXT_TELEMETRY_DISABLED: "1",
  NEWSROOM_DEPLOYMENT_PROFILE: "hosted",
  NEWSROOM_FRONTEND_SURFACE: "portal",
  NEWSROOM_PUBLIC_ORIGIN: publicOrigin,
  NEWSROOM_ALLOW_INSECURE_ORIGIN: "1",
  NEWSROOM_API_BASE_URL: apiOrigin,
} satisfies Record<string, string>

// The readiness spec selects itself from process.env at module evaluation time.
// Force the same fixed profile for Playwright workers and the production server.
Object.assign(process.env, hostedEnvironment)

export default defineConfig({
  testDir: path.join(configDir, "tests", "e2e"),
  testMatch: [
    "paper-agent-app.spec.ts",
    "paper-agent-settings.spec.ts",
    "research-readiness.spec.ts",
  ],
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  outputDir: path.join(configDir, "test-results", "paper-agent-production-profile"),
  use: {
    baseURL: publicOrigin,
    trace: "on-first-retry",
    storageState: {
      cookies: [
        {
          name: "newsroom_session",
          value: "paper-agent-e2e-session",
          domain: "127.0.0.1",
          path: "/",
          expires: -1,
          httpOnly: true,
          secure: false,
          sameSite: "Lax",
        },
      ],
      origins: [],
    },
  },
  projects: [
    {
      name: "paper-agent-desktop-chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: "node scripts/paper-agent-e2e-server.mjs",
    url: publicOrigin,
    env: hostedEnvironment,
    timeout: 15 * 60 * 1000,
    reuseExistingServer: false,
    gracefulShutdown: {
      signal: "SIGTERM",
      timeout: 15 * 1000,
    },
  },
})
