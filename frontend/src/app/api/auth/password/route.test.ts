import { NextRequest } from "next/server"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { safeApiPost } from "@/lib/api/server"
import { POST } from "./route"

vi.mock("@/lib/api/server", () => ({ safeApiPost: vi.fn() }))
const sessionCookie = vi.hoisted(() => ({ value: "current-session-token" as string | undefined }))
vi.mock("next/headers", () => ({
  cookies: async () => ({ get: () => sessionCookie.value ? { value: sessionCookie.value } : undefined }),
}))

const origin = "http://127.0.0.1:3120"
const credentials = { currentPassword: "old-password", newPassword: "new-password-1" }

function passwordRequest(body: unknown, headers: Record<string, string> = {}) {
  return new NextRequest(`${origin}/api/auth/password`, {
    method: "POST",
    headers: { Host: "127.0.0.1:3120", Origin: origin, "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body),
  })
}

describe("POST /api/auth/password", () => {
  beforeEach(() => {
    vi.mocked(safeApiPost).mockReset()
    sessionCookie.value = "current-session-token"
    vi.stubEnv("NEWSROOM_DEPLOYMENT_PROFILE", "hosted")
    vi.stubEnv("NEWSROOM_PUBLIC_ORIGIN", origin)
    vi.stubEnv("NEWSROOM_ALLOW_INSECURE_ORIGIN", "1")
  })

  it("forwards the session and public origin, then swaps in the rotated session cookie", async () => {
    vi.mocked(safeApiPost).mockResolvedValue({ ok: true, data: { session: { sessionToken: "rotated-session-token", expiresAt: "2026-12-31T00:00:00Z", user: { userId: "user-1" } } } })

    const response = await POST(passwordRequest(credentials))

    expect(response.status).toBe(200)
    expect(safeApiPost).toHaveBeenCalledWith("/api/v1/account/password", credentials, {
      headers: { "X-Newsroom-Session": "current-session-token", Origin: origin },
      signal: expect.any(AbortSignal),
    })
    expect(response.cookies.get("newsroom_session")?.value).toBe("rotated-session-token")
    expect(await response.text()).not.toContain("rotated-session-token")
    expect(response.headers.get("cache-control")).toBe("no-store")
  })

  it("rejects a foreign origin before reading the body or calling the API", async () => {
    const response = await POST(passwordRequest(credentials, { Origin: "https://evil.test", "Sec-Fetch-Site": "cross-site" }))
    expect(response.status).toBe(403)
    expect(safeApiPost).not.toHaveBeenCalled()
  })

  it("requires a signed-in session", async () => {
    sessionCookie.value = undefined
    const response = await POST(passwordRequest(credentials))
    expect(response.status).toBe(401)
    expect(await response.json()).toMatchObject({ error: { code: "auth_required" } })
    expect(safeApiPost).not.toHaveBeenCalled()
  })

  it.each([
    [{ ...credentials, userId: "someone-else" }],
    [{ currentPassword: "", newPassword: "new-password-1" }],
    [{ currentPassword: "old-password", newPassword: "short" }],
    [{ currentPassword: "old-password", newPassword: "x".repeat(257) }],
    [{ currentPassword: 1, newPassword: "new-password-1" }],
    [[]],
  ])("refuses malformed input %j without calling the API", async body => {
    const response = await POST(passwordRequest(body))
    expect(response.status).toBe(400)
    expect(await response.json()).toMatchObject({ error: { code: "auth_invalid_request" } })
    expect(safeApiPost).not.toHaveBeenCalled()
  })

  it("keeps the current cookie when the current password is wrong", async () => {
    vi.mocked(safeApiPost).mockResolvedValue({ ok: false, errorCode: "auth_invalid_credentials", errorMessage: "wrong" })
    const response = await POST(passwordRequest(credentials))
    expect(response.status).toBe(400)
    expect(await response.json()).toMatchObject({ error: { code: "auth_invalid_credentials" } })
    expect(response.cookies.get("newsroom_session")).toBeUndefined()
  })

  it("is unavailable outside the hosted profile", async () => {
    vi.stubEnv("NEWSROOM_DEPLOYMENT_PROFILE", "default")
    const response = await POST(passwordRequest(credentials))
    expect(response.status).toBe(404)
    expect(safeApiPost).not.toHaveBeenCalled()
  })
})
