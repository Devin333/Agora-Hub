import { beforeEach, describe, expect, it, vi } from "vitest"
import { NextRequest } from "next/server"
import { POST as compilePaper } from "@/app/api/papers/[paperId]/compile/route"
import { GET as getIngestOps, POST as triggerIngest } from "@/app/api/papers/ops/ingest/route"
import { safeApiGet, safeApiPost } from "@/lib/api/server"
import { getPaperById } from "@/lib/papers/real-data"
import type { Paper } from "@/lib/papers/types"

const cookieState = vi.hoisted(() => ({
  token: "session-token" as string | null,
}))

vi.mock("next/headers", () => ({
  cookies: async () => ({
    get: (name: string) => (name === "newsroom_session" && cookieState.token ? { value: cookieState.token } : undefined),
  }),
}))

vi.mock("@/lib/api/server", () => ({
  safeApiGet: vi.fn(),
  safeApiPost: vi.fn(),
}))

vi.mock("@/lib/papers/real-data", () => ({
  getPaperById: vi.fn(),
}))

const paper = {
  id: "paper-1",
  slug: "public-paper",
  title: "Public Paper",
  isPublished: true,
} as Paper

function request(path: string, init?: ConstructorParameters<typeof NextRequest>[1]) {
  return new NextRequest(new URL(`http://localhost${path}`), init)
}

async function responseJson<T>(response: Response): Promise<T> {
  return response.json() as Promise<T>
}

function sessionResult(role: string | null) {
  return {
    ok: true,
    data: {
      session: role ? { user: { role } } : null,
    },
  } as const
}

describe("paper ops route guard", () => {
  beforeEach(() => {
    cookieState.token = "session-token"
    vi.mocked(safeApiGet).mockReset()
    vi.mocked(safeApiPost).mockReset()
    vi.mocked(getPaperById).mockReset()
  })

  it("returns 401 before proxying when the session cookie is missing", async () => {
    cookieState.token = null

    const response = await triggerIngest(request("/api/papers/ops/ingest", { method: "POST", body: "{}" }))

    expect(response.status).toBe(401)
    await expect(responseJson(response)).resolves.toMatchObject({
      success: false,
      error: { code: "auth_session_required" },
    })
    expect(safeApiGet).not.toHaveBeenCalled()
    expect(safeApiPost).not.toHaveBeenCalled()
  })

  it("returns 401 for expired backend sessions instead of reporting an authorization failure", async () => {
    vi.mocked(safeApiGet).mockResolvedValueOnce(sessionResult(null))

    const response = await triggerIngest(request("/api/papers/ops/ingest", { method: "POST", body: "{}" }))

    expect(response.status).toBe(401)
    await expect(responseJson(response)).resolves.toMatchObject({
      success: false,
      error: { code: "auth_session_required" },
    })
    expect(safeApiPost).not.toHaveBeenCalled()
  })

  it("blocks non-ops roles before proxying paper operations", async () => {
    vi.mocked(safeApiGet).mockResolvedValueOnce(sessionResult("viewer"))

    const response = await triggerIngest(request("/api/papers/ops/ingest", { method: "POST", body: "{}" }))

    expect(response.status).toBe(403)
    await expect(responseJson(response)).resolves.toMatchObject({
      success: false,
      error: { code: "paper_ops_forbidden" },
    })
    expect(safeApiPost).not.toHaveBeenCalled()
  })

  it("allows operators to read ops state and proxies the backend request", async () => {
    vi.mocked(safeApiGet)
      .mockResolvedValueOnce(sessionResult("operator"))
      .mockResolvedValueOnce({ ok: true, data: { ingest: { runs: [] } } })

    const response = await getIngestOps(request("/api/papers/ops/ingest?limit=7"))

    expect(response.status).toBe(200)
    await expect(responseJson(response)).resolves.toMatchObject({
      success: true,
      data: { ingest: { runs: [] } },
    })
    expect(safeApiGet).toHaveBeenNthCalledWith(1, "/api/v1/auth/session", {
      headers: { "x-newsroom-session": "session-token" },
    })
    expect(safeApiGet).toHaveBeenNthCalledWith(2, "/api/v1/papers/ops/ingest?limit=7")
  })

  it("allows admins to compile a public paper through the canonical backend id", async () => {
    vi.mocked(getPaperById).mockResolvedValueOnce(paper)
    vi.mocked(safeApiGet).mockResolvedValueOnce(sessionResult("admin"))
    vi.mocked(safeApiPost).mockResolvedValueOnce({
      ok: true,
      data: { enqueued: { task_type: "papers.visual_compile", status: "queued" } },
    })

    const response = await compilePaper(
      request("/api/papers/public-paper/compile", {
        method: "POST",
        body: JSON.stringify({ force: true }),
      }),
      { params: Promise.resolve({ paperId: "public-paper" }) },
    )

    expect(response.status).toBe(200)
    expect(safeApiPost).toHaveBeenCalledWith("/api/v1/papers/paper-1/compile", { force: true })
  })
})
