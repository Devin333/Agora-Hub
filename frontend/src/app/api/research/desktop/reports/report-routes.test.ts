import { beforeEach, describe, expect, it, vi } from "vitest"
import { NextRequest } from "next/server"

vi.mock("@/lib/research/desktop/server-proxy", () => ({
  desktopGet: vi.fn(() => new Response(null, { status: 204 })),
  desktopFailure: vi.fn(() => new Response(null, { status: 400 })),
  safeDesktopId: vi.fn(() => true),
}))

import { desktopGet } from "@/lib/research/desktop/server-proxy"
import { GET } from "./[reportId]/route"

describe("saved report BFF route", () => {
  beforeEach(() => vi.clearAllMocks())

  it("forwards to the actual owner-scoped backend report route", async () => {
    const request = new NextRequest("http://127.0.0.1:3000/api/research/desktop/reports/report-1")
    await GET(request, { params: Promise.resolve({ reportId: "report-1" }) })
    expect(desktopGet).toHaveBeenCalledWith(
      "/api/v1/research/desktop/reports/report-1",
      request,
    )
  })
})
