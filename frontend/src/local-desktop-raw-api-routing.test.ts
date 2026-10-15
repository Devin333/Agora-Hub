import { spawnSync } from "node:child_process"
import { describe, expect, it } from "vitest"

type Rewrite = { source: string; destination: string }

describe("raw API build routing", () => {
  it("preserves the default deployment rewrite contract", () => {
    expect(loadRewrites("default", "http://127.0.0.1:8122")).toEqual([
      {
        source: "/api/v1/:path*",
        destination: "http://127.0.0.1:8122/api/v1/:path*",
      },
    ])
  })

  it("does not freeze the local-desktop API origin into the production manifest", () => {
    expect(loadRewrites("local-desktop", "http://127.0.0.1:8122")).toEqual([])
  })
})

function loadRewrites(
  deploymentProfile: "default" | "local-desktop",
  apiBaseUrl: string,
): Rewrite[] {
  const script = `
    import nextConfig from "./next.config.mjs"
    process.stdout.write(JSON.stringify(await nextConfig.rewrites()))
  `
  const result = spawnSync(process.execPath, ["--input-type=module", "--eval", script], {
    cwd: process.cwd(),
    encoding: "utf8",
    env: {
      ...process.env,
      NEWSROOM_API_BASE_URL: apiBaseUrl,
      NEWSROOM_CANONICAL_ORIGIN: "http://127.0.0.1:3124",
      NEWSROOM_DEPLOYMENT_PROFILE: deploymentProfile,
      NEWSROOM_DESKTOP_CANONICAL_ORIGIN: "http://127.0.0.1:3124",
    },
  })
  if (result.status !== 0) throw new Error(`Failed to load next.config.mjs:\n${result.stderr}`)
  return JSON.parse(result.stdout) as Rewrite[]
}
