import { spawnSync } from "node:child_process"
import { describe, expect, it } from "vitest"

type LoadedConfig = {
  configured: {
    hasCacheMaxMemorySize: boolean
    hasExperimental: boolean
    hasIsrFlushToDisk: boolean
    isrFlushToDisk?: boolean
  }
  defaults: {
    cacheMaxMemorySize: number
    isrFlushToDisk: boolean
  }
}

const loadConfig = (deploymentProfile: "default" | "local-desktop"): LoadedConfig => {
  const script = `
    import nextConfig from "./next.config.mjs"
    import { defaultConfig } from "next/dist/server/config-shared.js"

    process.stdout.write(JSON.stringify({
      configured: {
        hasCacheMaxMemorySize: Object.hasOwn(nextConfig, "cacheMaxMemorySize"),
        hasExperimental: Object.hasOwn(nextConfig, "experimental"),
        hasIsrFlushToDisk: Object.hasOwn(nextConfig.experimental ?? {}, "isrFlushToDisk"),
        isrFlushToDisk: nextConfig.experimental?.isrFlushToDisk,
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
      NEWSROOM_CANONICAL_ORIGIN: "http://127.0.0.1:3000",
      NEWSROOM_DEPLOYMENT_PROFILE: deploymentProfile,
      NEWSROOM_DESKTOP_CANONICAL_ORIGIN: "http://127.0.0.1:3000",
    },
  })

  if (result.status !== 0) {
    throw new Error(`Failed to load next.config.mjs:\n${result.stderr}`)
  }

  return JSON.parse(result.stdout) as LoadedConfig
}

describe("Next.js route cache configuration", () => {
  it("keeps the default profile on the installed Next.js cache defaults", () => {
    const config = loadConfig("default")

    expect(config.defaults).toEqual({
      cacheMaxMemorySize: 50 * 1024 * 1024,
      isrFlushToDisk: true,
    })
    expect(config.configured).toEqual({
      hasCacheMaxMemorySize: false,
      hasExperimental: false,
      hasIsrFlushToDisk: false,
    })
  })

  it("disables route-cache disk flush only for local-desktop", () => {
    const config = loadConfig("local-desktop")

    expect(config.defaults).toEqual({
      cacheMaxMemorySize: 50 * 1024 * 1024,
      isrFlushToDisk: true,
    })
    expect(config.configured).toEqual({
      hasCacheMaxMemorySize: false,
      hasExperimental: true,
      hasIsrFlushToDisk: true,
      isrFlushToDisk: false,
    })
  })
})