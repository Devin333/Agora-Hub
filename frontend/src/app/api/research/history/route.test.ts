import { beforeEach, describe, expect, it, vi } from "vitest"
import { NextRequest, NextResponse } from "next/server"
import { GET, PUT } from "@/app/api/research/history/route"
import { safeApiGet, safeApiPut } from "@/lib/api/server"
import { isSameOrigin } from "@/lib/auth/portal-server"

vi.mock("next/headers", () => ({
  cookies: () => ({ get: () => ({ value: "session-token" }) })
}))

vi.mock("@/lib/auth/portal-server", () => ({
  authFailure: (code: string, status: number) => NextResponse.json({ success: false, error: { code } }, { status }),
  isSameOrigin: vi.fn(() => true)
}))

vi.mock("@/lib/api/server", () => ({
  safeApiGet: vi.fn(),
  safeApiPut: vi.fn()
}))

describe("research history proxy", () => {
  beforeEach(() => {
    vi.mocked(safeApiGet).mockReset()
    vi.mocked(safeApiPut).mockReset()
    vi.mocked(isSameOrigin).mockReturnValue(true)
  })

  it("forwards the HttpOnly session server-side and preserves authentication failures", async () => {
    vi.mocked(safeApiGet).mockResolvedValueOnce({
      ok: false,
      errorCode: "auth_session_invalid",
      errorMessage: "Valid account session required",
      status: 401
    })

    const response = await GET()
    expect(response.status).toBe(401)
    expect((await response.json()).error.code).toBe("auth_session_invalid")
    expect(safeApiGet).toHaveBeenCalledWith("/api/v1/research/history", expect.objectContaining({
      headers: { "x-newsroom-session": "session-token" }
    }))
  })

  it("puts history without exposing a token to the browser request", async () => {
    vi.mocked(safeApiPut).mockResolvedValueOnce({ ok: true, data: { revision: 2, visits: [], groups: [] } })
    const body = { revision: 1, visits: [], groups: [] }
    const response = await PUT(new NextRequest("http://localhost/api/research/history", {
      method: "PUT",
      headers: { "origin": "http://localhost", "content-type": "application/json" },
      body: JSON.stringify(body)
    }))

    expect(response.status).toBe(200)
    expect((await response.json()).data.revision).toBe(2)
    expect(safeApiPut).toHaveBeenCalledWith("/api/v1/research/history", body, expect.objectContaining({
      headers: expect.objectContaining({ "x-newsroom-session": "session-token" })
    }))
    expect(JSON.stringify(vi.mocked(safeApiPut).mock.calls[0]?.[2])).not.toContain("sessionToken")
  })

  it("preserves compare-and-swap conflict status", async () => {
    vi.mocked(safeApiPut).mockResolvedValueOnce({
      ok: false,
      errorCode: "research_history_conflict",
      errorMessage: "reload required",
      status: 409
    })
    const response = await PUT(new NextRequest("http://localhost/api/research/history", {
      method: "PUT",
      headers: { "origin": "http://localhost", "content-type": "application/json" },
      body: JSON.stringify({ revision: 0, visits: [], groups: [] })
    }))
    expect(response.status).toBe(409)
    expect((await response.json()).error.code).toBe("research_history_conflict")
  })

  it("preserves upstream service-unavailable status and disables caching", async () => {
    vi.mocked(safeApiGet).mockResolvedValueOnce({
      ok: false,
      errorCode: "research_history_unavailable",
      errorMessage: "storage unavailable",
      status: 503
    })
    const response = await GET()
    expect(response.status).toBe(503)
    expect(response.headers.get("cache-control")).toBe("no-store")
  })

  it("rejects oversized payloads before forwarding", async () => {
    const response = await PUT(new NextRequest("http://localhost/api/research/history", {
      method: "PUT",
      headers: { "origin": "http://localhost", "content-length": String(4 * 1024 * 1024 + 1) },
      body: "{}"
    }))
    expect(response.status).toBe(413)
    expect(safeApiPut).not.toHaveBeenCalled()
  })

  it("rejects cross-origin writes before reading or forwarding the body", async () => {
    vi.mocked(isSameOrigin).mockReturnValueOnce(false)
    const response = await PUT(new NextRequest("http://localhost/api/research/history", {
      method: "PUT",
      headers: { "origin": "https://evil.example", "content-type": "application/json" },
      body: JSON.stringify({ revision: 0, visits: [], groups: [] })
    }))
    expect(response.status).toBe(403)
    expect(safeApiPut).not.toHaveBeenCalled()
  })
})
