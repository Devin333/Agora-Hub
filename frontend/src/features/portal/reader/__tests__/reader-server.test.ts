import { beforeEach, describe, expect, it, vi } from "vitest"
import { loadReaderWorkspace } from "../reader-server"
import { safeApiGet } from "@/lib/api/server"
import { getPaperById } from "@/lib/papers/real-data"
import type { Paper } from "@/lib/papers/types"
import { loadReaderSource } from "../reader-source-server"
import { compileArxivSourceHtml } from "../reader-source-compiler"
import { sourceHtml, sourcePaper } from "./source-fixture"

vi.mock("@/lib/api/server", () => ({ safeApiGet: vi.fn() }))
vi.mock("@/lib/papers/real-data", () => ({ getPaperById: vi.fn() }))
vi.mock("../reader-source-server", () => ({ loadReaderSource: vi.fn() }))
const paper = { id: "canonical-id", slug: "paper-slug", isPublished: true, title: "Public Paper", abstractSnippet: "Original abstract" } as Paper

describe("reader workspace server", () => {
  beforeEach(() => { vi.resetAllMocks(); vi.mocked(loadReaderSource).mockResolvedValue(null) })
  it("never substitutes analysis sections or an abstract for missing full text", async () => {
    vi.mocked(getPaperById).mockResolvedValue(paper)
    vi.mocked(safeApiGet).mockResolvedValue({ ok: false, errorCode: "research_configuration_invalid", errorMessage: "Unavailable" })
    const result = await loadReaderWorkspace("paper-slug")
    expect(safeApiGet).not.toHaveBeenCalled()
    expect(loadReaderSource).toHaveBeenCalledWith(paper)
    expect(result).toEqual({ paper, reader: null, state: "unavailable", reasonCode: "source_conversion_unavailable", aiReady: false })
  })
  it("publishes typed full text only after its independent source gate passes", async () => {
    vi.mocked(getPaperById).mockResolvedValue(sourcePaper)
    const payload = compileArxivSourceHtml(sourcePaper, sourceHtml(), "https://arxiv.org/html/2605.22343", "hash")!
    const source = { payload, assets: new Map(), expires: Date.now() + 1000, byteSize: 0 }
    vi.mocked(loadReaderSource).mockResolvedValue(source)
    expect(await loadReaderWorkspace(sourcePaper.id)).toMatchObject({ state: "needs_review", reader: null })
    payload.document!.status = "compiled"
    payload.status.status = "compiled"
    payload.status.gateReport = { passed: true }
    const result = await loadReaderWorkspace(sourcePaper.id)
    expect(result).toMatchObject({ state: "ready", sourceKind: "arxiv-html", aiReady: false })
    expect(result?.reader?.sections.at(-1)?.title).toBe("Appendix A")
    expect(safeApiGet).not.toHaveBeenCalled()
  })
  it.each([null, { ...paper, isPublished: false }])("never loads research for hidden or absent papers", async (input) => {
    vi.mocked(getPaperById).mockResolvedValue(input)
    expect(await loadReaderWorkspace("hidden")).toBeNull()
    expect(safeApiGet).not.toHaveBeenCalled()
  })
})
