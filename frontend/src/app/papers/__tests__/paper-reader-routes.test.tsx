import type { ReactElement } from "react"
import { describe, expect, it, vi } from "vitest"
import PaperDetailPageRoute from "@/app/papers/[slug]/page"
import PaperDocumentReadRoute from "@/app/papers/[slug]/read/page"
import { loadPaperDocumentPayload } from "@/lib/paper-reader/server-loader"
import { getPaperById } from "@/lib/papers/real-data"
import type { PaperDocumentResponse } from "@/lib/paper-reader/types"
import type { Paper } from "@/lib/papers/types"

vi.mock("next/navigation", () => ({
  notFound: vi.fn(() => {
    throw new Error("NEXT_NOT_FOUND")
  })
}))

vi.mock("@/app/papers/[slug]/paper-detail-page-client", () => ({
  PaperDetailPageClient: ({ paper }: { paper: Paper }) => <div data-testid="paper-detail-route">{paper.title}</div>
}))

vi.mock("@/lib/paper-reader/server-loader", () => ({
  loadPaperDocumentPayload: vi.fn()
}))

vi.mock("@/lib/papers/real-data", () => ({
  getPaperById: vi.fn()
}))

const paper: Paper = {
  id: "paper-special",
  slug: "agent paper/v2",
  title: "Agent Paper v2",
  abstractSnippet: "A published paper with a route-sensitive slug.",
  authors: ["NewsRoom"],
  publishedAt: "2026-05-30T00:00:00Z",
  tags: [],
  taskRefs: [],
  methodRefs: [],
  isPublished: true
}

const payload: PaperDocumentResponse = {
  paper,
  status: {
    paperId: paper.id,
    status: "queued",
    updatedAt: "2026-05-30T00:00:00Z",
    diagnostics: []
  },
  document: null,
  manifest: null,
  ai: {
    summary: null,
    signals: {
      abstractSnippet: paper.abstractSnippet,
      taskRefs: [],
      methodRefs: [],
      benchmarks: [],
      implementations: []
    },
    diagnostics: []
  }
}

describe("paper reader routes", () => {
  it("loads standalone detail pages with the decoded paper slug", async () => {
    vi.mocked(getPaperById).mockResolvedValueOnce(paper)

    const element = await PaperDetailPageRoute({ params: Promise.resolve({ slug: "agent%20paper%2Fv2" }) }) as ReactElement<{
      paper: Paper
    }>

    expect(getPaperById).toHaveBeenCalledWith("agent paper/v2")
    expect(element.props.paper).toBe(paper)
  })

  it("loads document payloads with the decoded paper slug", async () => {
    vi.mocked(loadPaperDocumentPayload).mockResolvedValueOnce(payload)

    const element = await PaperDocumentReadRoute({ params: Promise.resolve({ slug: "agent%20paper%2Fv2" }) }) as ReactElement<{
      payload: PaperDocumentResponse
    }>

    expect(loadPaperDocumentPayload).toHaveBeenCalledWith("agent paper/v2")
    expect(element.props.payload).toBe(payload)
  })
})
