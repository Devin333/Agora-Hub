import { NextRequest } from "next/server"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { GET } from "@/app/api/papers/route"
import { getPaperListResult } from "@/lib/papers/real-data"

vi.mock("@/lib/papers/real-data", () => ({
  getPaperListResult: vi.fn()
}))

const mockedGetPaperListResult = vi.mocked(getPaperListResult)

describe("paper list route", () => {
  beforeEach(() => {
    mockedGetPaperListResult.mockReset()
    mockedGetPaperListResult.mockResolvedValue({
      query: "",
      period: "all",
      sort: "trending",
      paper_count: 0,
      total_count: 0,
      source_count: 0,
      limit: 15,
      offset: 0,
      papers: []
    })
  })

  it("passes independent search, topic, date, and feature filters to the catalogue query", async () => {
    const request = new NextRequest(
      "http://localhost/api/papers?q=agent+planning&topic=cs.AI&from=2026-05-01&to=2026-05-31&has=pdf%2Ccode&sort=relevance&limit=20&offset=40"
    )

    const response = await GET(request)

    expect(response.status).toBe(200)
    expect(mockedGetPaperListResult).toHaveBeenCalledWith({
      q: "agent planning",
      period: undefined,
      sort: "relevance",
      topic: "cs.AI",
      from: "2026-05-01",
      to: "2026-05-31",
      task: undefined,
      method: undefined,
      has: "pdf,code",
      limit: 20,
      offset: 40
    })
  })

  it("ignores unsupported sort values rather than widening the sort contract", async () => {
    await GET(new NextRequest("http://localhost/api/papers?sort=semantic"))

    expect(mockedGetPaperListResult).toHaveBeenCalledWith(expect.objectContaining({ sort: undefined }))
  })
})
