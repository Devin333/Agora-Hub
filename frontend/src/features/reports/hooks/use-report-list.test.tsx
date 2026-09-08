import { renderHook, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { useReportList } from "@/features/reports/hooks/use-report-list"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import type { ReactNode } from "react"

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{children}</QueryClientProvider>
}

describe("useReportList", () => {
  afterEach(() => vi.unstubAllGlobals())

  it("uses the real report search endpoint for a homepage query", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ success: true, data: { reports: [{ report_id: "report-1", title: "Agent report", status: "published", created_at: "2026-09-01T00:00:00Z" }] } }),
    })
    vi.stubGlobal("fetch", fetchMock)

    const { result } = renderHook(() => useReportList({ keyword: "Agent memory" }), { wrapper })

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/search/reports?q=Agent%20memory&limit=50", expect.objectContaining({ cache: "no-store" }))
    expect(result.current.data.map((report) => report.id)).toEqual(["report-1"])
  })

  it("exposes the real API error without substituting mock reports", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("backend unavailable")))

    const { result } = renderHook(() => useReportList(), { wrapper })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(result.current.data).toEqual([])
    expect(result.current.error?.message).toBe("backend unavailable")
  })
})
