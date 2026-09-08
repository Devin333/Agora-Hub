import { describe, expect, it, vi, beforeEach } from "vitest"
import { NextRequest } from "next/server"
import { POST as askPaper } from "@/app/api/papers/[paperId]/ask/route"
import { GET as readPaper } from "@/app/api/papers/[paperId]/reader/route"
import { POST as recordReaderEvent } from "@/app/api/papers/[paperId]/reader/events/route"
import { POST as summarizePaper } from "@/app/api/papers/[paperId]/summary/route"
import { PATCH as patchPaperState } from "@/app/api/papers/[paperId]/state/route"
import { GET as getUserStates } from "@/app/api/papers/me/state/route"
import { safeApiGet, safeApiPatch, safeApiPost } from "@/lib/api/server"
import { getPaperById, getPublishedPapers } from "@/lib/papers/real-data"
import type { Paper } from "@/lib/papers/types"

vi.mock("@/lib/api/server", () => ({
  safeApiGet: vi.fn(),
  safeApiPatch: vi.fn(),
  safeApiPost: vi.fn(),
}))

vi.mock("@/lib/papers/real-data", () => ({
  getPaperById: vi.fn(),
  getPublishedPapers: vi.fn(),
}))

vi.mock("next/headers", () => ({
  cookies: () => ({
    get: () => ({ value: "session-token" }),
  }),
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

describe("paper public route guard", () => {
  beforeEach(() => {
    vi.mocked(safeApiGet).mockReset()
    vi.mocked(safeApiPatch).mockReset()
    vi.mocked(safeApiPost).mockReset()
    vi.mocked(getPaperById).mockReset()
    vi.mocked(getPublishedPapers).mockReset()
  })

  it("blocks unpublished or unknown papers before summary generation reaches the backend", async () => {
    vi.mocked(getPaperById).mockResolvedValueOnce(null)

    const response = await summarizePaper(request("/api/papers/draft-paper/summary?locale=en"), {
      params: { paperId: "draft-paper" },
    })

    expect(response.status).toBe(404)
    await expect(responseJson(response)).resolves.toMatchObject({
      success: false,
      error: { code: "paper_not_found" },
    })
    expect(safeApiPost).not.toHaveBeenCalled()
  })

  it("uses the canonical public paper id when a deep link calls a slug route", async () => {
    vi.mocked(getPaperById).mockResolvedValueOnce(paper)
    vi.mocked(safeApiPost).mockResolvedValueOnce({
      ok: true,
      data: { answer: "grounded", evidenceRefs: ["evidence:1"], confidence: 0.8 },
    })

    const response = await askPaper(
      request("/api/papers/public-paper/ask", {
        method: "POST",
        body: JSON.stringify({ question: "What changed?", locale: "en" }),
      }),
      { params: { paperId: "public-paper" } },
    )

    expect(response.status).toBe(200)
    expect(safeApiPost).toHaveBeenCalledWith("/api/v1/research/papers/paper-1/ask", {
      question: "What changed?",
      locale: "en",
    }, { signal: expect.any(AbortSignal) })
    await expect(response.json()).resolves.toMatchObject({ success: true, data: { answer: { paperId: "paper-1", answer: "grounded", citations: [{ id: "evidence:1", evidenceId: "evidence:1", sourceType: "evidence" }] } } })
  })

  it("does not proxy reader data for non-public paper refs", async () => {
    vi.mocked(getPaperById).mockResolvedValueOnce(null)

    const response = await readPaper(request("/api/papers/draft-paper/reader?locale=en"), {
      params: { paperId: "draft-paper" },
    })

    expect(response.status).toBe(404)
    expect(safeApiGet).not.toHaveBeenCalled()
  })

  it("rejects blank questions before running Research and malformed answers after it", async () => {
    vi.mocked(getPaperById).mockResolvedValue(paper)
    const blank = await askPaper(request("/api/papers/public-paper/ask", { method: "POST", body: JSON.stringify({ question: "  " }) }), { params: { paperId: "public-paper" } })
    expect(blank.status).toBe(400)
    expect(safeApiPost).not.toHaveBeenCalled()
    vi.mocked(safeApiPost).mockResolvedValueOnce({ ok: true, data: { answer: { invented: "wrong" } } })
    const malformed = await askPaper(request("/api/papers/public-paper/ask", { method: "POST", body: JSON.stringify({ question: "Explain" }) }), { params: { paperId: "public-paper" } })
    expect(malformed.status).toBe(502)
    expect(await malformed.json()).toMatchObject({ error: { code: "reader_answer_invalid" } })
  })

  it("filters bulk user states down to currently public papers", async () => {
    vi.mocked(getPaperById).mockResolvedValueOnce(paper).mockResolvedValueOnce(null)
    vi.mocked(safeApiGet).mockResolvedValueOnce({
      ok: true,
      data: {
        states: [
          { userId: "u1", paperId: "paper-1", favorite: true, subscribed: false, readingStatus: "reading", progressPercent: 50, updatedAt: "2026-01-01T00:00:00.000Z" },
          { userId: "u1", paperId: "draft-paper", favorite: true, subscribed: true, readingStatus: "unread", progressPercent: 0, updatedAt: "2026-01-01T00:00:00.000Z" },
        ],
      },
    })

    const response = await getUserStates(request("/api/papers/me/state?paperIds=public-paper,draft-paper"))

    expect(response.status).toBe(200)
    await expect(responseJson(response)).resolves.toMatchObject({
      success: true,
      data: { states: [{ paperId: "paper-1" }] },
    })
    expect(safeApiGet).toHaveBeenCalledWith("/api/v1/papers/me/state?paperIds=paper-1", {
      headers: { "x-newsroom-session": "session-token" },
    })
  })

  it("keeps backend paper-not-found errors as not found after the public guard passes", async () => {
    vi.mocked(getPaperById).mockResolvedValueOnce(paper)
    vi.mocked(safeApiPost).mockResolvedValueOnce({
      ok: false,
      errorCode: "paper_not_found",
      errorMessage: "paper material store is missing",
    })

    const response = await recordReaderEvent(
      request("/api/papers/public-paper/reader/events", {
        method: "POST",
        body: JSON.stringify({ type: "reader_progress_sampled" }),
      }),
      { params: { paperId: "public-paper" } },
    )

    expect(response.status).toBe(404)
    await expect(responseJson(response)).resolves.toMatchObject({
      success: false,
      error: { code: "paper_not_found" },
    })
  })

  it("keeps invalid paper interaction payloads as bad requests", async () => {
    vi.mocked(getPaperById).mockResolvedValueOnce(paper)
    vi.mocked(safeApiPatch).mockResolvedValueOnce({
      ok: false,
      errorCode: "paper_state_invalid",
      errorMessage: "invalid state patch",
    })

    const response = await patchPaperState(
      request("/api/papers/public-paper/state", {
        method: "PATCH",
        body: JSON.stringify({ progressPercent: 200 }),
      }),
      { params: { paperId: "public-paper" } },
    )

    expect(response.status).toBe(400)
    await expect(responseJson(response)).resolves.toMatchObject({
      success: false,
      error: { code: "paper_state_invalid" },
    })
  })
})
