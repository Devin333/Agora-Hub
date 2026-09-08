import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { safeApiGet } from "@/lib/api/server"
import {
  mockApiEmpty,
  mockApiError,
  mockApiNetworkFailure,
  mockApiPartial,
  mockApiSuccess
} from "@/test/mock-api"
import { studioRunFixtures } from "@/test/fixtures/studio-runs"

const originalFetch = globalThis.fetch

describe("server api fallback surface", () => {
  beforeEach(() => {
    vi.stubEnv("NEWSROOM_API_BASE_URL", "http://newsroom-api.test")
  })

  afterEach(() => {
    vi.unstubAllEnvs()
    vi.restoreAllMocks()
    globalThis.fetch = originalFetch
  })

  it("unwraps success envelopes", async () => {
    const fetchMock = vi.fn().mockResolvedValue(mockApiSuccess({ runs: studioRunFixtures }))
    globalThis.fetch = fetchMock

    const result = await safeApiGet<{ runs: typeof studioRunFixtures }>("/api/v1/runs")

    expect(result).toEqual({ ok: true, data: { runs: studioRunFixtures } })
    expect(fetchMock).toHaveBeenCalledWith(
      "http://newsroom-api.test/api/v1/runs",
      expect.objectContaining({
        method: "GET",
        cache: "no-store",
        headers: { Accept: "application/json" }
      })
    )
  })

  it("normalizes error envelopes with request ids", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      mockApiError(
        {
          code: "studio_backend_unavailable",
          message: "Studio backend unavailable",
          details: { route: "/api/v1/runs" }
        },
        { requestId: "req-error-envelope" }
      )
    )

    const result = await safeApiGet("/api/v1/runs")

    expect(result).toEqual({
      ok: false,
      errorCode: "studio_backend_unavailable",
      errorMessage: "Studio backend unavailable",
      requestId: "req-error-envelope",
      status: 200
    })
  })

  it("normalizes network failures for fallback callers", async () => {
    globalThis.fetch = vi.fn().mockImplementation(() => mockApiNetworkFailure("connection refused"))

    const result = await safeApiGet("/api/v1/runs")

    expect(result).toEqual({
      ok: false,
      errorCode: "request_failed",
      errorMessage: "connection refused"
    })
  })

  it("passes partial data through for adapters to mark partial state", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(mockApiPartial({ runs: studioRunFixtures.slice(0, 1) }))

    const result = await safeApiGet<{
      runs: typeof studioRunFixtures
      dataState: string
      notices: string[]
    }>("/api/v1/runs")

    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.data.dataState).toBe("partial")
      expect(result.data.notices).toContain("Partial data returned by test mock API.")
      expect(result.data.runs).toHaveLength(1)
    }
  })

  it("passes empty list envelopes through for empty-state callers", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(mockApiEmpty())

    const result = await safeApiGet<{
      items: unknown[]
      total: number
      hasNext: boolean
    }>("/api/v1/boards")

    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.data.items).toEqual([])
      expect(result.data.total).toBe(0)
      expect(result.data.hasNext).toBe(false)
    }
  })
})
