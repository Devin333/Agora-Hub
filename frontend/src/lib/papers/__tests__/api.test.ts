import { beforeEach, describe, expect, it, vi } from "vitest"
import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api/client"
import {
  askPaper,
  createPaperReaderNote,
  deletePaperReaderNote,
  fetchPaperDetail,
  fetchPaperGraph,
  fetchPaperMethods,
  fetchPaperReaderPayload,
  fetchPaperReaderNotes,
  fetchPaperRelated,
  fetchPaperSections,
  fetchPaperUserState,
  fetchPaperUserStates,
  fetchPapers,
  fetchPaperTasks,
  patchPaperUserState,
  patchPaperReaderNote,
  refreshPaperSummary,
  requestPaperSummary
} from "@/lib/papers/api"

vi.mock("@/lib/api/client", () => ({
  apiGet: vi.fn(),
  apiDelete: vi.fn(),
  apiPatch: vi.fn(),
  apiPost: vi.fn()
}))

describe("paper browser API client", () => {
  beforeEach(() => {
    vi.mocked(apiGet).mockReset()
    vi.mocked(apiDelete).mockReset()
    vi.mocked(apiPatch).mockReset()
    vi.mocked(apiPost).mockReset()
  })

  it("uses BFF routes for list, detail, summary, reader payload, derived surfaces, state, and ask", async () => {
    vi.mocked(apiGet).mockResolvedValueOnce({ success: true, data: { papers: [] } })
    await fetchPapers({ limit: 10, period: "weekly" })
    expect(apiGet).toHaveBeenCalledWith("/api/papers?limit=10&period=weekly", undefined)

    vi.mocked(apiGet).mockResolvedValueOnce({ success: true, data: { paper: { id: "p" } } })
    await fetchPaperDetail("paper/1")
    expect(apiGet).toHaveBeenCalledWith("/api/papers/paper%2F1", undefined)

    vi.mocked(apiPost).mockResolvedValueOnce({ success: true, data: { summary: { paperId: "p" } } })
    await requestPaperSummary("paper/1", "en")
    expect(apiPost).toHaveBeenCalledWith("/api/papers/paper%2F1/summary?locale=en", undefined, undefined)

    vi.mocked(apiPost).mockResolvedValueOnce({ success: true, data: { summary: { paperId: "p" } } })
    await refreshPaperSummary("paper/1", "en", "stale summary")
    expect(apiPost).toHaveBeenCalledWith(
      "/api/papers/paper%2F1/summary?locale=en&refresh=true",
      { reason: "stale summary" },
      undefined
    )

    vi.mocked(apiGet).mockResolvedValueOnce({ success: true, data: { reader: { paper: { id: "p" } } } })
    await fetchPaperReaderPayload("paper/1", "en")
    expect(apiGet).toHaveBeenCalledWith("/api/papers/paper%2F1/reader?locale=en", undefined)

    vi.mocked(apiGet).mockResolvedValueOnce({ success: true, data: { sections: [] } })
    await fetchPaperSections("paper/1", "en")
    expect(apiGet).toHaveBeenCalledWith("/api/papers/paper%2F1/sections?locale=en", undefined)

    vi.mocked(apiGet).mockResolvedValueOnce({ success: true, data: { relatedPapers: [] } })
    await fetchPaperRelated("paper/1")
    expect(apiGet).toHaveBeenCalledWith("/api/papers/paper%2F1/related", undefined)

    vi.mocked(apiGet).mockResolvedValueOnce({ success: true, data: { graph: { paperId: "p", nodes: [], edges: [] } } })
    await fetchPaperGraph("paper/1")
    expect(apiGet).toHaveBeenCalledWith("/api/papers/paper%2F1/graph", undefined)

    vi.mocked(apiGet).mockResolvedValueOnce({ success: true, data: { tasks: [] } })
    await fetchPaperTasks()
    expect(apiGet).toHaveBeenCalledWith("/api/papers/tasks", undefined)

    vi.mocked(apiGet).mockResolvedValueOnce({ success: true, data: { methods: [] } })
    await fetchPaperMethods()
    expect(apiGet).toHaveBeenCalledWith("/api/papers/methods", undefined)

    vi.mocked(apiGet).mockResolvedValueOnce({
      success: true,
      data: {
        state: {
          userId: "user-1",
          paperId: "paper/1",
          favorite: false,
          subscribed: false,
          readingStatus: "unread",
          progressPercent: 0,
          updatedAt: "2026-05-24T00:00:00Z"
        }
      }
    })
    await fetchPaperUserState("paper/1")
    expect(apiGet).toHaveBeenCalledWith("/api/papers/paper%2F1/state", undefined)

    vi.mocked(apiPatch).mockResolvedValueOnce({
      success: true,
      data: {
        state: {
          userId: "user-1",
          paperId: "paper/1",
          favorite: true,
          subscribed: false,
          readingStatus: "reading",
          progressPercent: 25,
          updatedAt: "2026-05-24T00:00:00Z"
        }
      }
    })
    await patchPaperUserState("paper/1", { favorite: true, readingStatus: "reading", progressPercent: 25 })
    expect(apiPatch).toHaveBeenCalledWith(
      "/api/papers/paper%2F1/state",
      { favorite: true, readingStatus: "reading", progressPercent: 25 },
      undefined
    )

    vi.mocked(apiGet).mockResolvedValueOnce({ success: true, data: { states: [] } })
    await fetchPaperUserStates(["paper/1", "paper/2"])
    expect(apiGet).toHaveBeenCalledWith("/api/papers/me/state?paperIds=paper%2F1%2Cpaper%2F2", undefined)

    vi.mocked(apiGet).mockResolvedValueOnce({ success: true, data: { notes: [] } })
    await fetchPaperReaderNotes("paper/1")
    expect(apiGet).toHaveBeenCalledWith("/api/papers/paper%2F1/notes", undefined)

    vi.mocked(apiPost).mockResolvedValueOnce({ success: true, data: { note: { noteId: "n1" } } })
    await createPaperReaderNote("paper/1", { kind: "bookmark", pageNumber: 1 })
    expect(apiPost).toHaveBeenCalledWith(
      "/api/papers/paper%2F1/notes",
      { kind: "bookmark", pageNumber: 1 },
      undefined
    )

    vi.mocked(apiPatch).mockResolvedValueOnce({ success: true, data: { note: { noteId: "n1" } } })
    await patchPaperReaderNote("paper/1", "note/1", { color: "pink" })
    expect(apiPatch).toHaveBeenCalledWith(
      "/api/papers/paper%2F1/notes/note%2F1",
      { color: "pink" },
      undefined
    )

    vi.mocked(apiDelete).mockResolvedValueOnce({ success: true, data: { deleted: true } })
    await deletePaperReaderNote("paper/1", "note/1")
    expect(apiDelete).toHaveBeenCalledWith("/api/papers/paper%2F1/notes/note%2F1", undefined)

    vi.mocked(apiPost).mockResolvedValueOnce({ success: true, data: { answer: { paperId: "p" } } })
    await askPaper("paper/1", "What is new?", "en")
    expect(apiPost).toHaveBeenCalledWith(
      "/api/papers/paper%2F1/ask",
      { question: "What is new?", locale: "en" },
      undefined
    )
  })

  it("serializes independent discovery filters for the paper list", async () => {
    vi.mocked(apiGet).mockResolvedValueOnce({ success: true, data: { papers: [] } })

    await fetchPapers({
      q: "agent planning",
      topic: "cs.AI",
      from: "2026-05-01",
      to: "2026-05-31",
      has: ["pdf", "code"],
      sort: "relevance"
    })

    expect(apiGet).toHaveBeenCalledWith(
      "/api/papers?q=agent+planning&topic=cs.AI&from=2026-05-01&to=2026-05-31&has=pdf%2Ccode&sort=relevance",
      undefined
    )
  })
})
