import { beforeEach, describe, expect, it, vi } from "vitest"
import { apiGet, apiPost } from "@/lib/api/client"
import { bootstrapAccount, fetchAuthSession, login, logout } from "@/lib/auth/api"
import { portalSessionChangedEvent } from "@/lib/auth/session-events"

vi.mock("@/lib/api/client", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn()
}))

const session = {
  user: { userId: "user-1", username: "admin", role: "admin" as const },
  sessionId: "sess-1",
  expiresAt: "2026-06-01T00:00:00Z"
}

describe("auth API client", () => {
  beforeEach(() => {
    vi.mocked(apiGet).mockReset()
    vi.mocked(apiPost).mockReset()
  })

  it("uses auth BFF routes for session, bootstrap, login, and logout", async () => {
    vi.mocked(apiGet).mockResolvedValueOnce({ success: true, data: { initialized: true, session: null } })
    await fetchAuthSession()
    expect(apiGet).toHaveBeenCalledWith("/api/auth/session", undefined)

    vi.mocked(apiPost).mockResolvedValueOnce({ success: true, data: { session } })
    await bootstrapAccount("admin", "correct horse")
    expect(apiPost).toHaveBeenCalledWith(
      "/api/auth/bootstrap",
      { username: "admin", password: "correct horse" },
      undefined
    )

    vi.mocked(apiPost).mockResolvedValueOnce({ success: true, data: { session: { ...session, sessionId: "sess-2" } } })
    await login("admin", "correct horse")
    expect(apiPost).toHaveBeenCalledWith(
      "/api/auth/login",
      { username: "admin", password: "correct horse" },
      undefined
    )

    vi.mocked(apiPost).mockResolvedValueOnce({ success: true, data: { revoked: true } })
    await logout()
    expect(apiPost).toHaveBeenCalledWith("/api/auth/logout", undefined, undefined)
  })

  it("notifies the mounted account owner only after successful auth mutations", async () => {
    const changed = vi.fn()
    window.addEventListener(portalSessionChangedEvent, changed)
    try {
      vi.mocked(apiPost)
        .mockResolvedValueOnce({ success: true, data: { session } })
        .mockResolvedValueOnce({ success: true, data: { session } })
        .mockResolvedValueOnce({ success: true, data: { revoked: true } })
        .mockResolvedValueOnce({ success: false, error: { code: "auth_invalid_credentials", message: "invalid username or password" } })
        .mockResolvedValueOnce({ success: false, error: { code: "auth_logout_failed", message: "logout failed" } })

      await bootstrapAccount("admin", "correct horse")
      await login("admin", "correct horse")
      await logout()
      expect(changed).toHaveBeenCalledTimes(3)

      await expect(login("admin", "wrong horse")).rejects.toThrow("invalid username or password")
      await expect(logout()).rejects.toThrow("logout failed")
      expect(changed).toHaveBeenCalledTimes(3)
    } finally {
      window.removeEventListener(portalSessionChangedEvent, changed)
    }
  })

  it("surfaces auth errors without raw backend payloads", async () => {
    vi.mocked(apiPost).mockResolvedValueOnce({
      success: false,
      error: {
        code: "auth_invalid_credentials",
        message: "invalid username or password",
        requestId: "req-1"
      }
    })

    await expect(login("admin", "wrong horse")).rejects.toThrow("invalid username or password")
  })
})
