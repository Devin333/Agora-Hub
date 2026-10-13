import { describe, expect, it } from "vitest"
import { desktopCanonicalOrigin, isConfiguredDesktopOrigin, resolveDeploymentProfile, validateLocalDesktopServiceOrigin } from "@/lib/deployment-profile"

describe("desktop deployment profile", () => {
  it("recognizes only the explicit local desktop profile", () => {
    expect(resolveDeploymentProfile("local-desktop")).toBe("local-desktop")
    expect(() => resolveDeploymentProfile("desktop")).toThrow(/Unsupported/)
  })

  it("accepts loopback origins and rejects non-loopback origins", () => {
    expect(desktopCanonicalOrigin("http://127.0.0.1:3000")).toBe("http://127.0.0.1:3000")
    expect(() => desktopCanonicalOrigin("http://0.0.0.0:3000")).toThrow(/loopback/)
    expect(() => desktopCanonicalOrigin("http://127.0.0.1:3000/app")).toThrow(/only scheme/)
  })

  it("supports a custom loopback port and rejects conflicting origin aliases", () => {
    expect(desktopCanonicalOrigin("http://localhost:4317")).toBe("http://localhost:4317")
    const canonical = process.env.NEWSROOM_CANONICAL_ORIGIN
    const legacy = process.env.NEWSROOM_DESKTOP_CANONICAL_ORIGIN
    process.env.NEWSROOM_CANONICAL_ORIGIN = "http://127.0.0.1:4317"
    process.env.NEWSROOM_DESKTOP_CANONICAL_ORIGIN = "http://127.0.0.1:4318"
    try {
      expect(() => desktopCanonicalOrigin()).toThrow(/must match/)
    } finally {
      if (canonical === undefined) delete process.env.NEWSROOM_CANONICAL_ORIGIN
      else process.env.NEWSROOM_CANONICAL_ORIGIN = canonical
      if (legacy === undefined) delete process.env.NEWSROOM_DESKTOP_CANONICAL_ORIGIN
      else process.env.NEWSROOM_DESKTOP_CANONICAL_ORIGIN = legacy
    }
  })

  it("does not trust a claimed origin outside the configured desktop profile", () => {
    const previous = process.env.NEWSROOM_DEPLOYMENT_PROFILE
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "local-desktop"
    try {
      expect(isConfiguredDesktopOrigin("http://127.0.0.1:3000")).toBe(true)
      expect(isConfiguredDesktopOrigin("http://localhost:3000")).toBe(false)
    } finally {
      if (previous === undefined) delete process.env.NEWSROOM_DEPLOYMENT_PROFILE
      else process.env.NEWSROOM_DEPLOYMENT_PROFILE = previous
    }
  })
  it("accepts only a pure loopback HTTP(S) API origin", () => {
    expect(validateLocalDesktopServiceOrigin("http://127.0.0.1:8000")).toBe("http://127.0.0.1:8000")
    for (const value of [
      "file://127.0.0.1/service",
      "http://user:secret@127.0.0.1:8000",
      "http://127.0.0.1:8000/api",
      "http://127.0.0.1:8000?mode=desktop",
      "http://127.0.0.1:8000#ready",
    ]) expect(() => validateLocalDesktopServiceOrigin(value)).toThrow()
  })

})
