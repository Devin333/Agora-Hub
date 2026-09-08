import { NextRequest } from "next/server"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { safeApiGet, safeApiPost } from "@/lib/api/server"
import { GET as methods } from "@/app/api/auth/methods/route"
import { POST as requestCode } from "@/app/api/auth/otp/challenges/route"
import { POST as verify } from "@/app/api/auth/otp/challenges/[challengeId]/verify/route"
import { POST as authorize } from "@/app/api/auth/oauth/[provider]/authorize/route"
import { GET as callback, POST as frameCallback } from "@/app/api/auth/oauth/[provider]/callback/route"
import { GET as session } from "@/app/api/auth/session/route"
import { POST as logout } from "@/app/api/auth/logout/route"
import { authResponse, isSameOrigin, safeReturnPath } from "./portal-server"

vi.mock("@/lib/api/server", () => ({ safeApiGet: vi.fn(), safeApiPost: vi.fn() }))
vi.mock("next/headers", () => ({ cookies: () => ({ get: () => ({ value: "private-session-token" }) }) }))
const binding = "a".repeat(43)
const origin = "http://localhost:3000"
function post(path: string, body: unknown, headers: Record<string, string> = {}) {
  return new NextRequest(`${origin}${path}`, { method: "POST", headers: { Origin: origin, "Content-Type": "application/json", Cookie: `newsroom_auth_binding=${binding}`, ...headers }, body: JSON.stringify(body) })
}
function callbackRequest(query: string, cookies = "newsroom_auth_callback=valid-state; newsroom_auth_return=%2Fdesign-demo%2Fpapers%3Fq%3DAgent") {
  return new NextRequest(`${origin}/api/auth/oauth/google/callback?${query}`, { headers: { Cookie: `newsroom_auth_binding=${binding}; ${cookies}` } })
}

describe("public auth BFF", () => {
  beforeEach(() => { vi.mocked(safeApiGet).mockReset(); vi.mocked(safeApiPost).mockReset() })
  it("issues a private browser binding when fetching noncached method capabilities", async () => {
    vi.mocked(safeApiGet).mockResolvedValue({ ok: true, data: { methods: {} } })
    const result = await methods(new NextRequest(`${origin}/api/auth/methods`))
    expect(result.cookies.get("newsroom_auth_binding")?.value).toMatch(/^[\w-]{43}$/)
    expect(result.headers.get("set-cookie")).toContain("HttpOnly")
    expect(result.headers.get("cache-control")).toBe("no-store")
  })
  it("rejects foreign origins and missing browser bindings before calling a backend", async () => {
    for (const headers of [{ Origin: "https://evil.test" }, { Origin: "null" }, { Cookie: "" }] as Record<string, string>[]) {
      const response = await requestCode(post("/api/auth/otp/challenges", { channel: "email", destination: "a@example.com", consent: true }, headers))
      expect(response.status).toBe(403)
    }
    expect(safeApiPost).not.toHaveBeenCalled()
  })
  it("uses the browser-facing host when Next normalizes its internal URL", () => {
    const request = new NextRequest(`${origin}/api/auth/logout`, { headers: { Host: "127.0.0.1:3000", Origin: "http://127.0.0.1:3000", "Sec-Fetch-Site": "same-origin" } })
    expect(isSameOrigin(request)).toBe(true)
    const foreign = new NextRequest(`${origin}/api/auth/logout`, { headers: { Host: "127.0.0.1:3000", Origin: "http://localhost:3000" } })
    expect(isSameOrigin(foreign)).toBe(false)
  })
  it("passes only validated challenge fields and a server-owned browser binding", async () => {
    vi.mocked(safeApiPost).mockResolvedValue({ ok: true, data: { challengeId: "challenge_0123456789", expiresAt: "future", resendAt: "future" } })
    const response = await requestCode(post("/api/auth/otp/challenges", { channel: "email", destination: "a@example.com", consent: true, role: "admin", binding: "attacker" }))
    expect(response.status).toBe(200)
    expect(safeApiPost).toHaveBeenCalledWith("/api/v1/auth/otp/challenges", { channel: "email", destination: "a@example.com", consent: true }, expect.objectContaining({ headers: { "X-Newsroom-Auth-Binding": binding } }))
  })
  it("requires consent and rejects unsafe path components", async () => {
    const response = await requestCode(post("/api/auth/otp/challenges", { channel: "email", destination: "a@example.com" }))
    expect(response.status).toBe(400)
    expect((await verify(post("/", { code: "123456" }), { params: { challengeId: "../../bootstrap" } })).status).toBe(400)
    expect(safeApiPost).not.toHaveBeenCalled()
  })
  it("writes verified tokens only into HttpOnly cookies and retains backend error secrecy", async () => {
    vi.mocked(safeApiPost).mockResolvedValue({ ok: true, data: { session: { user: { role: "user" }, sessionToken: "secret-token" } } })
    const response = await verify(post("/", { code: "123456" }), { params: { challengeId: "challenge_0123456789" } })
    expect(await response.text()).not.toContain("secret-token")
    expect(response.cookies.get("newsroom_session")?.value).toBe("secret-token")
    expect(response.headers.get("set-cookie")).toContain("HttpOnly")
    const failed = authResponse({ ok: false, errorCode: "request_failed", errorMessage: "SMTP_PASSWORD=secret" })
    expect(await failed.text()).not.toContain("secret")
    expect(failed.status).toBe(503)
    expect(authResponse({ ok: true, data: { session: { user: {} } } }).status).toBe(502)
  })
  it("stores an OAuth transaction state and a validated local return path", async () => {
    vi.mocked(safeApiPost).mockResolvedValue({ ok: true, data: { authorizationUrl: "https://accounts.google.com/o/oauth2/v2/auth?state=valid-state", expiresAt: "future" } })
    const response = await authorize(post("/", { consent: true, returnTo: "/design-demo/papers?q=Agent#results" }), { params: { provider: "google" } })
    expect(response.cookies.get("newsroom_auth_callback")?.value).toBe("valid-state")
    expect(response.cookies.get("newsroom_auth_return")?.value).toBe("/design-demo/papers?q=Agent#results")
  })
  it("refuses callback state mismatch before exchanging code", async () => {
    const response = await callback(callbackRequest("code=code&state=wrong-state"), { params: { provider: "google" } })
    expect(safeApiPost).not.toHaveBeenCalled()
    expect(response.cookies.get("newsroom_session")).toBeUndefined()
    expect(await response.text()).toContain('"success":false')
  })
  it("completes OAuth with CSP and no token in HTML, and deletes consumed state", async () => {
    vi.mocked(safeApiPost).mockResolvedValue({ ok: true, data: { session: { sessionToken: "private-session-token" } } })
    const response = await callback(callbackRequest("code=code&state=valid-state"), { params: { provider: "google" } })
    const html = await response.text()
    expect(html).toContain("agora-auth-complete")
    expect(html).toContain('"success":true')
    expect(html).not.toContain("private-session-token")
    expect(response.cookies.get("newsroom_session")?.value).toBe("private-session-token")
    expect(response.cookies.get("newsroom_auth_callback")?.value).toBe("")
    expect(response.headers.get("content-security-policy")).toContain("frame-ancestors 'self'")
    expect(response.headers.get("referrer-policy")).toBe("no-referrer")
  })
  it("does not interpolate untrusted callback strings into script markup", async () => {
    const response = await callback(callbackRequest(`state=${encodeURIComponent('</script><script>alert(1)</script>')}`), { params: { provider: "google" } })
    expect(await response.text()).not.toContain("</script><script>alert")
  })
  it("relays a cross-site WeChat frame response before using browser-bound credentials", async () => {
    const request = new NextRequest(`${origin}/api/auth/oauth/wechat/callback?code=wechat-code&state=valid-state`, { headers: { "Sec-Fetch-Dest": "iframe" } })
    const response = await callback(request, { params: { provider: "wechat" } })
    expect(await response.text()).toContain('method="post" action="/api/auth/oauth/wechat/callback"')
    expect(safeApiPost).not.toHaveBeenCalled()
    expect(response.cookies.get("newsroom_session")).toBeUndefined()
    expect(response.headers.get("content-security-policy")).toContain("form-action 'self'")
    const formRequest = (requestOrigin: string) => new NextRequest(`${origin}/api/auth/oauth/wechat/callback`, { method: "POST", headers: { Origin: requestOrigin, Cookie: `newsroom_auth_binding=${binding}; newsroom_auth_callback=valid-state` }, body: new URLSearchParams({ code: "wechat-code", state: "valid-state" }) })
    expect((await frameCallback(formRequest("https://evil.test"), { params: { provider: "wechat" } })).status).toBe(403)
    expect(safeApiPost).not.toHaveBeenCalled()
    vi.mocked(safeApiPost).mockResolvedValue({ ok: true, data: { session: { sessionToken: "verified-wechat-session" } } })
    const completed = await frameCallback(formRequest(origin), { params: { provider: "wechat" } })
    expect(completed.cookies.get("newsroom_session")?.value).toBe("verified-wechat-session")
    expect(await completed.text()).not.toContain("verified-wechat-session")
  })
  it("returns a bounded retry signal for rate limiting", async () => {
    const response = authResponse({ ok: false, errorCode: "auth_rate_limited", errorMessage: "raw upstream", retryAfter: 3600 })
    expect(response.status).toBe(429)
    expect(response.headers.get("retry-after")).toBe("3600")
  })
  it("reads an existing session without requiring or returning its bearer token", async () => {
    vi.mocked(safeApiGet).mockResolvedValue({ ok: true, data: { session: { user: { role: "user" } } } })
    const result = await session()
    expect(result.status).toBe(200)
    expect(await result.json()).toMatchObject({ data: { session: { user: { role: "user" } } } })
    expect(result.headers.get("cache-control")).toBe("no-store")
    expect(result.cookies.get("newsroom_session")).toBeUndefined()
  })
  it("clears expired session cookies but retains them on a service outage", async () => {
    vi.mocked(safeApiGet).mockResolvedValueOnce({ ok: true, data: { session: null } }).mockResolvedValueOnce({ ok: false, errorCode: "request_failed", errorMessage: "private host error" })
    expect((await session()).cookies.get("newsroom_session")?.value).toBe("")
    const failed = await session()
    expect(failed.cookies.get("newsroom_session")).toBeUndefined()
    expect(await failed.text()).not.toContain("private host")
  })
  it("revokes only same-origin logout requests and preserves the session if revocation fails", async () => {
    expect((await logout(post("/api/auth/logout", {}, { Origin: "https://other.test" }))).status).toBe(403)
    expect(safeApiPost).not.toHaveBeenCalled()
    vi.mocked(safeApiPost).mockResolvedValueOnce({ ok: false, errorCode: "request_failed", errorMessage: "private" }).mockResolvedValueOnce({ ok: true, data: { revoked: true } })
    expect((await logout(post("/api/auth/logout", {}))).cookies.get("newsroom_session")).toBeUndefined()
    expect((await logout(post("/api/auth/logout", {}))).cookies.get("newsroom_session")?.value).toBe("")
  })
  it("blocks external, encoded, backslash and login-loop returns", () => {
    for (const path of ["//evil.test", "https://evil.test", "/\\evil.test", "/%5cevil.test", "/%2fevil.test", "/api/auth/callback", "/login?next=/", "/%0aevil", null]) expect(safeReturnPath(path)).toBe("/design-demo")
    expect(safeReturnPath("/design-demo/papers?q=agent&has=code#results")).toBe("/design-demo/papers?q=agent&has=code#results")
  })
})
