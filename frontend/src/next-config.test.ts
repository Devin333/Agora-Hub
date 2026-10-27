import { spawnSync } from "node:child_process"
import { describe, expect, it } from "vitest"

type LoadedConfig = {
  configured: {
    hasCacheMaxMemorySize: boolean
    hasExperimental: boolean
    hasIsrFlushToDisk: boolean
    isrFlushToDisk?: boolean
    output?: string
    rewrites: unknown[]
  }
  defaults: {
    cacheMaxMemorySize: number
    isrFlushToDisk: boolean
  }
}

const loadConfig = (deploymentProfile: "default" | "hosted"): LoadedConfig => {
  const script = `
    import nextConfig from "./next.config.mjs"
    import { defaultConfig } from "next/dist/server/config-shared.js"

    process.stdout.write(JSON.stringify({
      configured: {
        hasCacheMaxMemorySize: Object.hasOwn(nextConfig, "cacheMaxMemorySize"),
        hasExperimental: Object.hasOwn(nextConfig, "experimental"),
        hasIsrFlushToDisk: Object.hasOwn(nextConfig.experimental ?? {}, "isrFlushToDisk"),
        isrFlushToDisk: nextConfig.experimental?.isrFlushToDisk,
        output: nextConfig.output,
        rewrites: await nextConfig.rewrites(),
      },
      defaults: {
        cacheMaxMemorySize: defaultConfig.cacheMaxMemorySize,
        isrFlushToDisk: defaultConfig.experimental.isrFlushToDisk,
      },
    }))
  `
  const result = spawnSync(process.execPath, ["--input-type=module", "--eval", script], {
    cwd: process.cwd(),
    encoding: "utf8",
    env: {
      ...process.env,
      NEWSROOM_API_BASE_URL: "http://127.0.0.1:8000",
      NEWSROOM_DEPLOYMENT_PROFILE: deploymentProfile,
      NEWSROOM_PUBLIC_ORIGIN: "https://papers.example.com",
    },
  })

  if (result.status !== 0) {
    throw new Error(`Failed to load next.config.mjs:\n${result.stderr}`)
  }

  return JSON.parse(result.stdout) as LoadedConfig
}

describe("Next.js deployment configuration", () => {
  it("keeps the default profile on the installed cache defaults and the API rewrite", () => {
    const config = loadConfig("default")

    expect(config.defaults).toEqual({
      cacheMaxMemorySize: 50 * 1024 * 1024,
      isrFlushToDisk: true,
    })
    expect(config.configured).toMatchObject({
      hasCacheMaxMemorySize: false,
      hasExperimental: false,
      hasIsrFlushToDisk: false,
    })
    expect(config.configured.output).toBeUndefined()
    expect(config.configured.rewrites).toHaveLength(1)
  })

  it("builds the hosted profile as a standalone server with no raw API rewrite", () => {
    const config = loadConfig("hosted")

    expect(config.configured).toMatchObject({ hasExperimental: false, output: "standalone" })
    expect(config.configured.rewrites).toEqual([])
  })
})
