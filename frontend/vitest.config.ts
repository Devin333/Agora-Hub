import path from "node:path"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vitest/config"
import { loadRootEnv } from "./root-env"

const configDir = process.cwd()

loadRootEnv(configDir)

export default defineConfig({
  cacheDir: path.resolve(configDir, "../.tmp/vitest/frontend"),
  plugins: [react()],
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    exclude: ["node_modules/**", ".next/**", "tests/e2e/**"],
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
  },
  resolve: {
    alias: {
      "@": path.resolve(configDir, "./src"),
    },
  },
})
