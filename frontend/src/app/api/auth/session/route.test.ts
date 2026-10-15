import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { safeApiGet } from "@/lib/api/server"
import { GET } from "./route"

const sessionCookie = vi.hoisted(() => ({ value: undefined as string | undefined }))

vi.mock("next/headers", () => ({
  cookies: async () => ({
    get: () => sessionCookie.value ? { value: sessionCookie.value } : undefined,
  }),
}))

vi.mock("@/lib/api/server", () => ({ safeApiGet: vi.fn() }))

const runtimeEnvKeys = [
  "NEWSROOM_DEPLOYMENT_PROFILE",
  "NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE",
  "NEWSROOM_DESKTOP_INSTANCE_KEY",
  "NEWSROOM_DESKTOP_RELEASE_ID",
  "NEWSROOM_DESKTOP_CONFIG_DIGEST",
  "NEWSROOM_DESKTOP_BOOT_ID",
] as const
const originalRuntimeEnv = Object.fromEntries(runtimeEnvKeys.map(key => [key, process.env[key]]))

function configureDesktopRuntime() {
  process.env.NEWSROOM_DEPLOYMENT_PROFILE = "local-desktop"
  process.env.NEWSROOM_DESKTOP_INSTANCE_KEY = "local-desktop:http://127.0.0.1:3120"
  process.env.NEWSROOM_DESKTOP_RELEASE_ID = "release-0.1.1"
  process.env.NEWSROOM_DESKTOP_CONFIG_DIGEST = "a".repeat(64)
  process.env.NEWSROOM_DESKTOP_BOOT_ID = "b".repeat(32)
}

describe("GET /api/auth/session", () => {
  beforeEach(() => {
    vi.mocked(safeApiGet).mockReset()
    sessionCookie.value = undefined
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "default"
    delete process.env.NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE
    delete process.env.NEWSROOM_DESKTOP_INSTANCE_KEY
    delete process.env.NEWSROOM_DESKTOP_RELEASE_ID
    delete process.env.NEWSROOM_DESKTOP_CONFIG_DIGEST
    delete process.env.NEWSROOM_DESKTOP_BOOT_ID
  })

  afterEach(() => {
    for (const key of runtimeEnvKeys) {
      const value = originalRuntimeEnv[key]
      if (value === undefined) delete process.env[key]
      else process.env[key] = value
    }
  })

  it.each([false, true])("forwards the real initialized=%s state without a session cookie", async initialized => {
    vi.mocked(safeApiGet).mockResolvedValue({ ok: true, data: { initialized, session: null } })

    const response = await GET()

    expect(response.status).toBe(200)
    expect(await response.json()).toEqual({ success: true, data: { initialized, session: null } })
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

  it("preserves initialized and attaches only the frontend-owned runtime identity", async () => {
    configureDesktopRuntime()
    vi.mocked(safeApiGet).mockResolvedValue({
      ok: true,
      data: { initialized: false, session: null, local_desktop: { boot_id: "spoofed" } },
    })

    const response = await GET()

    expect(response.status).toBe(200)
    expect(await response.json()).toEqual({
      success: true,
      data: {
        initialized: false,
        session: null,
        local_desktop: {
          deployment_profile: "local-desktop",
          instance_key: "local-desktop:http://127.0.0.1:3120",
          release_id: "release-0.1.1",
          config_digest: "a".repeat(64),
          boot_id: "b".repeat(32),
        },
      },
    })
  })

  it("fails closed when the local-desktop runtime identity is incomplete", async () => {
    configureDesktopRuntime()
    delete process.env.NEWSROOM_DESKTOP_BOOT_ID
    vi.mocked(safeApiGet).mockResolvedValue({ ok: true, data: { initialized: true, session: null } })

    const response = await GET()

    expect(response.status).toBe(503)
    expect(await response.json()).toEqual({
      success: false,
      error: { code: "desktop_runtime_identity_invalid" },
    })
  })
})
