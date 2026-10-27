import { beforeEach, describe, expect, it, vi } from "vitest"
import { safeApiGet } from "@/lib/api/server"
import { GET } from "./route"

const sessionCookie = vi.hoisted(() => ({ value: undefined as string | undefined }))

vi.mock("next/headers", () => ({
  cookies: async () => ({
    get: () => sessionCookie.value ? { value: sessionCookie.value } : undefined,
  }),
}))

vi.mock("@/lib/api/server", () => ({ safeApiGet: vi.fn() }))

describe("GET /api/auth/session", () => {
  beforeEach(() => {
    vi.mocked(safeApiGet).mockReset()
    sessionCookie.value = undefined
  })

  it.each([
    [false, true],
    [true, false],
    // A hosted service is uninitialized until the CLI creates the first administrator,
    // but it never offers browser bootstrap.
    [false, false],
  ])("forwards initialized=%s and bootstrapAvailable=%s without a session cookie", async (initialized, bootstrapAvailable) => {
    vi.mocked(safeApiGet).mockResolvedValue({ ok: true, data: { initialized, bootstrapAvailable, session: null } })

    const response = await GET()

    expect(response.status).toBe(200)
    expect(await response.json()).toEqual({ success: true, data: { initialized, bootstrapAvailable, session: null } })
    expect(response.headers.get("cache-control")).toBe("no-store")
    expect(safeApiGet).toHaveBeenCalledWith("/api/v1/auth/session", {
      headers: undefined,
      signal: expect.any(AbortSignal),
    })
  })

  it("returns a sanitized service failure when the backend session endpoint fails", async () => {
    vi.mocked(safeApiGet).mockResolvedValue({
      ok: false,
      errorCode: "request_failed",
      errorMessage: "private backend host and stack",
    })

    const response = await GET()
    const text = await response.text()

    expect(response.status).toBe(503)
    expect(JSON.parse(text)).toEqual({ success: false, error: { code: "auth_service_unavailable" } })
    expect(text).not.toContain("private backend host")
  })

  it("uses the cookie only as an upstream credential and never returns a session token", async () => {
    sessionCookie.value = "browser-private-session"
    vi.mocked(safeApiGet).mockResolvedValue({
      ok: true,
      data: {
        initialized: true,
        session: {
          sessionToken: "backend-private-session",
          expiresAt: "2099-01-01T00:00:00Z",
          user: { role: "user" },
        },
      },
    })

    const response = await GET()
    const text = await response.text()

    expect(response.status).toBe(200)
    expect(JSON.parse(text)).toEqual({
      success: true,
      data: {
        initialized: true,
        session: {
          expiresAt: "2099-01-01T00:00:00Z",
          user: { role: "user" },
        },
      },
    })
    expect(text).not.toContain("browser-private-session")
    expect(text).not.toContain("backend-private-session")
    expect(response.cookies.get("newsroom_session")).toBeUndefined()
    expect(safeApiGet).toHaveBeenCalledWith("/api/v1/auth/session", {
      headers: { "x-newsroom-session": "browser-private-session" },
      signal: expect.any(AbortSignal),
    })
  })

  it("clears a stale session cookie when the backend no longer recognizes it", async () => {
    sessionCookie.value = "revoked-session"
    vi.mocked(safeApiGet).mockResolvedValue({ ok: true, data: { initialized: true, bootstrapAvailable: false, session: null } })

    const response = await GET()

    expect(response.status).toBe(200)
    expect(response.headers.get("set-cookie")).toMatch(/newsroom_session=;/)
  })
})
