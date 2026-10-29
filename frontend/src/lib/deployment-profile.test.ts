import { afterEach, describe, expect, it } from "vitest"
import { resolveApiBaseUrl, resolvePublicOrigin } from "../../deployment-config.mjs"
import {
  hostedAllowsInsecureCookie,
  isHostedProfile,
  resolveDeploymentProfile,
  serverOwnedMutationOriginHeaders,
} from "@/lib/deployment-profile"

const KEYS = ["NEWSROOM_DEPLOYMENT_PROFILE", "NEWSROOM_PUBLIC_ORIGIN", "NEWSROOM_ALLOW_INSECURE_ORIGIN"] as const
const saved = Object.fromEntries(KEYS.map(key => [key, process.env[key]]))

afterEach(() => {
  for (const key of KEYS) {
    if (saved[key] === undefined) delete process.env[key]
    else process.env[key] = saved[key]
  }
})

describe("deployment profile", () => {
  it("accepts only the default and hosted profiles", () => {
    expect(resolveDeploymentProfile("default")).toBe("default")
    expect(resolveDeploymentProfile("hosted")).toBe("hosted")
    expect(resolveDeploymentProfile(undefined)).toBe("default")
    expect(() => resolveDeploymentProfile("local-desktop")).toThrow(/Unsupported/)
  })

  it("requires an https public origin unless insecure integration is explicit", () => {
    expect(resolvePublicOrigin({ NEWSROOM_PUBLIC_ORIGIN: "https://papers.example.com/" })).toBe("https://papers.example.com")
    expect(() => resolvePublicOrigin({})).toThrow(/required/)
    expect(() => resolvePublicOrigin({ NEWSROOM_PUBLIC_ORIGIN: "http://127.0.0.1:3000" })).toThrow(/https/)
    expect(resolvePublicOrigin({ NEWSROOM_PUBLIC_ORIGIN: "http://127.0.0.1:3000", NEWSROOM_ALLOW_INSECURE_ORIGIN: "1" })).toBe("http://127.0.0.1:3000")
    expect(() => resolvePublicOrigin({ NEWSROOM_PUBLIC_ORIGIN: "http://127.0.0.1:3000", NEWSROOM_ALLOW_INSECURE_ORIGIN: "maybe" })).toThrow(/1 or 0/)
    for (const value of [
      "https://papers.example.com/app",
      "https://user:secret@papers.example.com",
      "https://papers.example.com?x=1",
      "https://papers.example.com#top",
      "papers.example.com",
    ]) expect(() => resolvePublicOrigin({ NEWSROOM_PUBLIC_ORIGIN: value })).toThrow()
  })

  it("accepts only a pure API origin", () => {
    expect(resolveApiBaseUrl({ NEWSROOM_API_BASE_URL: "http://api:8000" })).toBe("http://api:8000")
    expect(() => resolveApiBaseUrl({ NEWSROOM_API_BASE_URL: "http://api:8000/api" })).toThrow()
    expect(() => resolveApiBaseUrl({ NEWSROOM_API_BASE_URL: "file:///srv/api" })).toThrow()
  })

  it("stamps server-owned mutations with the configured public origin only when hosted", () => {
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "default"
    expect(serverOwnedMutationOriginHeaders()).toEqual({})
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "hosted"
    process.env.NEWSROOM_PUBLIC_ORIGIN = "https://papers.example.com"
    expect(isHostedProfile()).toBe(true)
    expect(serverOwnedMutationOriginHeaders()).toEqual({ Origin: "https://papers.example.com" })
    expect(hostedAllowsInsecureCookie()).toBe(false)
    process.env.NEWSROOM_PUBLIC_ORIGIN = "http://127.0.0.1:3000"
    process.env.NEWSROOM_ALLOW_INSECURE_ORIGIN = "1"
    expect(hostedAllowsInsecureCookie()).toBe(true)
  })
})
