import path from "node:path"
import { pathToFileURL } from "node:url"
import { describe, expect, it } from "vitest"
import { decideNavigation, isAllowedApplicationUrl, isTrustedIpcSource, paperAgentUrl } from "../src/navigation-policy"

const origin = "http://127.0.0.1:3120"
const startup = path.resolve("out/startup.html")

describe("navigation policy", () => {
  it("allows only the paper agent and login on the exact canonical origin", () => {
    expect(isAllowedApplicationUrl(`${origin}/paper-agent`, origin)).toBe(true)
    expect(isAllowedApplicationUrl(`${origin}/paper-agent/library?id=1`, origin)).toBe(true)
    expect(isAllowedApplicationUrl(`${origin}/login`, origin)).toBe(true)
    expect(isAllowedApplicationUrl(`${origin}/admin`, origin)).toBe(false)
    expect(isAllowedApplicationUrl("http://localhost:3120/paper-agent", origin)).toBe(false)
  })

  it("sends credentialless HTTP links external and denies dangerous schemes or credentials", () => {
    expect(decideNavigation("https://example.org/paper", origin)).toEqual({ action: "external", url: "https://example.org/paper" })
    expect(decideNavigation(`${origin}/admin`, origin)).toEqual({ action: "deny" })
    expect(decideNavigation("https://user:secret@example.org/paper", origin)).toEqual({ action: "deny" })
    expect(decideNavigation("file:///C:/Windows/win.ini", origin)).toEqual({ action: "deny" })
    expect(decideNavigation("javascript:alert(1)", origin)).toEqual({ action: "deny" })
  })

  it("checks every IPC source against the exact startup file or canonical app route", () => {
    expect(isTrustedIpcSource(pathToFileURL(startup).href, null, startup)).toBe(true)
    expect(isTrustedIpcSource(pathToFileURL(path.resolve("out/other.html")).href, null, startup)).toBe(false)
    expect(isTrustedIpcSource(`${origin}/paper-agent`, origin, startup)).toBe(true)
    expect(isTrustedIpcSource("https://evil.example/paper-agent", origin, startup)).toBe(false)
    expect(paperAgentUrl(origin)).toBe(`${origin}/paper-agent`)
  })
})
