import path from "node:path"
import { defineConfig, devices } from "@playwright/test"

const configDir = process.cwd()
const canonicalOrigin = "http://127.0.0.1:3317"
const apiOrigin = "http://127.0.0.1:8317"

const localDesktopEnvironment = {
  NEXT_TELEMETRY_DISABLED: "1",
  NEWSROOM_DEPLOYMENT_PROFILE: "local-desktop",
  NEWSROOM_FRONTEND_SURFACE: "portal",
  NEWSROOM_CANONICAL_ORIGIN: canonicalOrigin,
  NEWSROOM_DESKTOP_CANONICAL_ORIGIN: canonicalOrigin,
  NEWSROOM_PORTAL_ORIGIN: canonicalOrigin,
  NEWSROOM_API_BASE_URL: apiOrigin,
  NEXT_PUBLIC_NEWSROOM_API_BASE_URL: apiOrigin,
  NEXT_PUBLIC_API_BASE_URL: apiOrigin,
  NEWSROOM_DESKTOP_INSTANCE_KEY: `local-desktop:${canonicalOrigin}`,
  NEWSROOM_DESKTOP_RELEASE_ID: "paper-agent-e2e",
  NEWSROOM_DESKTOP_CONFIG_DIGEST: "e".repeat(64),
  NEWSROOM_DESKTOP_BOOT_ID: "e".repeat(32),
} satisfies Record<string, string>

// The readiness spec selects itself from process.env at module evaluation time.
// Force the same fixed profile for Playwright workers and the production server.
Object.assign(process.env, localDesktopEnvironment)

export default defineConfig({
  testDir: path.join(configDir, "tests", "e2e"),
  testMatch: [
    "paper-agent-app.spec.ts",
    "paper-agent-settings.spec.ts",
    "desktop-research-readiness.spec.ts",
  ],
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  outputDir: path.join(configDir, "test-results", "paper-agent-production-profile"),
  use: {
    baseURL: canonicalOrigin,
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
    url: canonicalOrigin,
    env: localDesktopEnvironment,
    timeout: 15 * 60 * 1000,
    reuseExistingServer: false,
    gracefulShutdown: {
      signal: "SIGTERM",
      timeout: 15 * 1000,
    },
  },
})
