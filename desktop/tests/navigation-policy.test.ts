import path from "node:path"
import { pathToFileURL } from "node:url"
import { describe, expect, it } from "vitest"
import { connectionPageUrl, decideNavigation, isConnectionPage, isWorkspaceUrl } from "../src/navigation-policy"

const origin = "https://agora.example.com"
const connectionFile = path.resolve("out/connect.html")

describe("navigation policy", () => {
  it("treats only login and the paper agent on the exact origin as the workspace", () => {
    expect(isWorkspaceUrl(`${origin}/paper-agent`, origin)).toBe(true)
    expect(isWorkspaceUrl(`${origin}/paper-agent/library?id=1`, origin)).toBe(true)
    expect(isWorkspaceUrl(`${origin}/login?next=%2Fpaper-agent`, origin)).toBe(true)
    expect(isWorkspaceUrl(`${origin}/paper-agentx`, origin)).toBe(false)
    expect(isWorkspaceUrl(`${origin}/admin`, origin)).toBe(false)
    expect(isWorkspaceUrl("https://agora.example.com:8443/paper-agent", origin)).toBe(false)
    expect(isWorkspaceUrl("http://agora.example.com/paper-agent", origin)).toBe(false)
    expect(isWorkspaceUrl("https://user:secret@agora.example.com/paper-agent", origin)).toBe(false)
  })

  it("recognizes exactly the packaged connection page", () => {
    expect(isConnectionPage(connectionPageUrl(connectionFile), connectionFile)).toBe(true)
    expect(isConnectionPage(`${connectionPageUrl(connectionFile)}#retry`, connectionFile)).toBe(true)
    expect(isConnectionPage(pathToFileURL(path.resolve("out/other.html")).href, connectionFile)).toBe(false)
    expect(isConnectionPage("file:///C:/Windows/win.ini", connectionFile)).toBe(false)
  })

  it("keeps the window on the connection page or the workspace and sends other web links out", () => {
    expect(decideNavigation(connectionPageUrl(connectionFile), null, connectionFile)).toEqual({ action: "allow" })
    expect(decideNavigation(`${origin}/paper-agent`, origin, connectionFile)).toEqual({ action: "allow" })
    expect(decideNavigation(`${origin}/paper-agent`, null, connectionFile)).toEqual({ action: "external", url: `${origin}/paper-agent` })
    expect(decideNavigation("https://arxiv.org/abs/2401.00001", origin, connectionFile)).toEqual({ action: "external", url: "https://arxiv.org/abs/2401.00001" })
    expect(decideNavigation(`${origin}/admin`, origin, connectionFile)).toEqual({ action: "deny" })
  })

  it("denies dangerous schemes and links carrying credentials", () => {
    expect(decideNavigation("https://user:secret@example.org/paper", origin, connectionFile)).toEqual({ action: "deny" })
    expect(decideNavigation("file:///C:/Windows/win.ini", origin, connectionFile)).toEqual({ action: "deny" })
    expect(decideNavigation("javascript:alert(1)", origin, connectionFile)).toEqual({ action: "deny" })
    expect(decideNavigation("not a url", origin, connectionFile)).toEqual({ action: "deny" })
  })
})
