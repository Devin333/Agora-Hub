import path from "node:path"
import { defineConfig } from "@playwright/test"

export default defineConfig({
  testDir: path.join(__dirname, "e2e"),
  timeout: 600_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  outputDir: path.join(__dirname, "test-results")
})
