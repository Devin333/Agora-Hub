import { NextRequest } from "next/server"
import { afterEach, describe, expect, it } from "vitest"
import { proxy } from "@/proxy"

function request(path: string, cookie?: string) {
  return new NextRequest(`http://localhost${path}`, {
    headers: cookie ? { cookie } : undefined,
  })
}

describe("auth proxy", () => {
  const originalSurface = process.env.NEWSROOM_FRONTEND_SURFACE
  const originalPortalOrigin = process.env.NEWSROOM_PORTAL_ORIGIN
  const originalAdminOrigin = process.env.NEWSROOM_ADMIN_ORIGIN
  const originalEnableFrontendAuth = process.env.NEWSROOM_ENABLE_FRONTEND_AUTH
  const originalDeploymentProfile = process.env.NEWSROOM_DEPLOYMENT_PROFILE

  afterEach(() => {
    restoreEnv("NEWSROOM_FRONTEND_SURFACE", originalSurface)
    restoreEnv("NEWSROOM_PORTAL_ORIGIN", originalPortalOrigin)
    restoreEnv("NEWSROOM_ADMIN_ORIGIN", originalAdminOrigin)
    restoreEnv("NEWSROOM_ENABLE_FRONTEND_AUTH", originalEnableFrontendAuth)
    restoreEnv("NEWSROOM_DEPLOYMENT_PROFILE", originalDeploymentProfile)
  })

  it("keeps anonymous Research read routes public", () => {
    process.env.NEWSROOM_FRONTEND_SURFACE = "portal"

    expect(proxy(request("/papers")).headers.get("location")).toBeNull()
    expect(proxy(request("/papers/reader-paper?panel=summary")).headers.get("location")).toBeNull()
    expect(proxy(request("/papers/reader-paper/read")).headers.get("location")).toBeNull()
    expect(proxy(request("/papers/tasks/agents")).headers.get("location")).toBeNull()
    expect(proxy(request("/papers/methods/tool-use")).headers.get("location")).toBeNull()
  })

  it("allows protected routes when the session cookie exists", () => {
    process.env.NEWSROOM_FRONTEND_SURFACE = "portal"
    const response = proxy(request("/papers", "newsroom_session=session-token"))

    expect(response.headers.get("location")).toBeNull()
  })

  it("keeps the Portal root public", () => {
    process.env.NEWSROOM_FRONTEND_SURFACE = "portal"

    const response = proxy(request("/"))

    expect(response.headers.get("location")).toBeNull()
  })

  it("bypasses login while frontend auth is temporarily disabled", () => {
    expect(proxy(request("/login")).headers.get("location")).toBe("http://localhost/")
    expect(proxy(request("/login?next=%2Freports")).headers.get("location")).toBe("http://localhost/reports")
    expect(proxy(request("/login?next=https%3A%2F%2Fexample.com")).headers.get("location")).toBe("http://localhost/")
    expect(proxy(request("/api/auth/session")).headers.get("location")).toBeNull()
  })

  it("allows anonymous Portal routes while frontend auth is temporarily disabled", () => {
    process.env.NEWSROOM_FRONTEND_SURFACE = "portal"

    expect(proxy(request("/reports")).headers.get("location")).toBeNull()
    expect(proxy(request("/news")).headers.get("location")).toBeNull()
    expect(proxy(request("/topics")).headers.get("location")).toBeNull()
    expect(proxy(request("/search")).headers.get("location")).toBeNull()
  })

  it("restores the login gate when frontend auth is explicitly enabled", () => {
    process.env.NEWSROOM_FRONTEND_SURFACE = "portal"
    process.env.NEWSROOM_ENABLE_FRONTEND_AUTH = "true"

    expect(proxy(request("/reports")).headers.get("location")).toBe("http://localhost/login?next=%2Freports")
    expect(proxy(request("/login")).headers.get("location")).toBeNull()
  })

  it("redirects management routes away from the Portal surface", () => {
    process.env.NEWSROOM_FRONTEND_SURFACE = "portal"

    const response = proxy(request("/studio/runs"))

    expect(response.status).toBe(307)
    expect(response.headers.get("location")).toBe("http://localhost/")
  })

  it("redirects Portal routes away from the Admin surface when an origin is configured", () => {
    process.env.NEWSROOM_FRONTEND_SURFACE = "admin"
    process.env.NEWSROOM_PORTAL_ORIGIN = "http://localhost:3001"

    const response = proxy(request("/papers?period=weekly", "newsroom_session=session-token"))

    expect(response.status).toBe(307)
    expect(response.headers.get("location")).toBe("http://localhost:3001/papers?period=weekly")
  })

  it("allows Studio in Admin surface mode while frontend auth is temporarily disabled", () => {
    process.env.NEWSROOM_FRONTEND_SURFACE = "admin"

    expect(proxy(request("/")).headers.get("location")).toBe("http://localhost/studio")
    expect(proxy(request("/studio/runs")).headers.get("location")).toBeNull()
  })

  it("keeps authenticated Admin root redirecting to Studio", () => {
    process.env.NEWSROOM_FRONTEND_SURFACE = "admin"

    const response = proxy(request("/", "newsroom_session=session-token"))

    expect(response.headers.get("location")).toBe("http://localhost/studio")
  })
  it("requires a session for every formal local-desktop route even when the legacy auth toggle is false", () => {
    process.env.NEWSROOM_FRONTEND_SURFACE = "portal"
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "local-desktop"
    process.env.NEWSROOM_ENABLE_FRONTEND_AUTH = "false"
    expect(proxy(request("/")).headers.get("location")).toBe("http://localhost/login?next=%2F")
    expect(proxy(request("/papers/2601.01234/read?resumePage=2")).headers.get("location")).toBe("http://localhost/login?next=%2Fpapers%2F2601.01234%2Fread%3FresumePage%3D2")
    expect(proxy(request("/reports", "newsroom_session=session-token")).headers.get("location")).toBeNull()
  })

  it("permanently normalizes the allowed legacy desktop route and preserves its query", () => {
    process.env.NEWSROOM_FRONTEND_SURFACE = "portal"
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "local-desktop"
    const response = proxy(request("/design-demo/papers/2601.01234/read?resumeSection=methods"))
    expect(response.status).toBe(308)
    expect(response.headers.get("location")).toBe("http://localhost/papers/2601.01234/read?resumeSection=methods")
  })
})

function restoreEnv(name: string, value: string | undefined) {
  if (value === undefined) {
    delete process.env[name]
    return
  }
  process.env[name] = value
}
