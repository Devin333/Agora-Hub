import { afterEach, describe, expect, it, vi } from "vitest"
import { apiGet, shouldUseMock } from "@/lib/api/client"

const originalFetch = globalThis.fetch

describe("browser api client", () => {
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.restoreAllMocks()
    globalThis.fetch = originalFetch
  })

  it("keeps Next BFF /api routes same-origin even when a public backend base URL is set", async () => {
    vi.stubEnv("NEXT_PUBLIC_NEWSROOM_API_BASE_URL", "http://127.0.0.1:8000")
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: { initialized: true } }))
    globalThis.fetch = fetchMock

    await apiGet("/api/auth/session")

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/auth/session",
      expect.objectContaining({ method: "GET" })
    )
  })

  it("still supports explicit absolute URLs", async () => {
    vi.stubEnv("NEXT_PUBLIC_NEWSROOM_API_BASE_URL", "http://127.0.0.1:8000")
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: { ok: true } }))
    globalThis.fetch = fetchMock

    await apiGet("https://api.example.test/status")

    expect(fetchMock).toHaveBeenCalledWith(
      "https://api.example.test/status",
      expect.objectContaining({ method: "GET" })
    )
  })

  it("uses an explicit mock only when the resolver owns the requested path", async () => {
    vi.stubEnv("NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE", "default")
    vi.stubEnv("NEXT_PUBLIC_NEWSROOM_USE_MOCKS", "true")
    const fetchMock = vi.fn()
    globalThis.fetch = fetchMock

    const result = await apiGet<{ items: unknown[] }>("/news")

    expect(result.items.length).toBeGreaterThan(0)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it("falls through to the real request path when mock mode does not own an endpoint", async () => {
    vi.stubEnv("NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE", "default")
    vi.stubEnv("NEXT_PUBLIC_NEWSROOM_USE_MOCKS", "true")
    const response = { success: true, data: { projects: [] } }
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(response))
    globalThis.fetch = fetchMock

    await expect(apiGet("/api/v1/projects")).resolves.toEqual(response)
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/projects",
      expect.objectContaining({ method: "GET" })
    )
  })

  it("disables browser mock data in the hosted service", () => {
    vi.stubEnv("NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE", "hosted")
    vi.stubEnv("NEXT_PUBLIC_NEWSROOM_USE_MOCKS", "true")

    expect(shouldUseMock()).toBe(false)
  })

  it("preserves the existing mock switch in the default profile", () => {
    vi.stubEnv("NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE", "default")
    vi.stubEnv("NEXT_PUBLIC_NEWSROOM_USE_MOCKS", "true")

    expect(shouldUseMock()).toBe(true)
  })
})

function jsonResponse(payload: unknown) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "content-type": "application/json" }
  })
}
