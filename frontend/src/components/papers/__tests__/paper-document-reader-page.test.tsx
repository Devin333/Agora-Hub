import { fireEvent, render, screen, within } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { PaperDocumentReaderPage } from "@/components/papers/paper-reader"
import { PaperDocumentReaderPageClient } from "@/app/papers/[slug]/read/paper-document-reader-page-client"
import { EquationRenderer, InlineMathRenderer } from "@/components/papers/open-reader/equation-renderer"
import { targetForPaperBlock } from "@/lib/paper-reader/interactions"
import { fetchPaperDocument, triggerPaperCompile } from "@/lib/paper-reader/api"
import type { PaperDocumentResponse } from "@/lib/paper-reader/types"
import { useUiStore } from "@/stores/ui-store"

vi.mock("@/lib/papers/api", () => ({
  fetchReaderMaterials: vi.fn().mockResolvedValue({
    paperId: "visual-paper",
    userId: "reader-user",
    selections: [],
    events: [],
    stats: {
      noteCount: 0,
      explainedCount: 0,
      exampledCount: 0,
      confusedCount: 0,
      materialCount: 0,
    },
  }),
  recordReaderEvent: vi.fn().mockResolvedValue({}),
}))

vi.mock("@/lib/paper-reader/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/paper-reader/api")>()
  return {
    ...actual,
    fetchPaperDocument: vi.fn(),
    triggerPaperCompile: vi.fn(),
  }
})

describe("PaperDocumentReaderPage", () => {
  beforeEach(() => {
    vi.mocked(fetchPaperDocument).mockReset()
    vi.mocked(fetchPaperDocument).mockRejectedValue(new Error("document is still preparing"))
    vi.mocked(triggerPaperCompile).mockReset()
  })

  it("uses the current UI locale when rendered from the read route client", () => {
    useUiStore.setState({ locale: "en" })

    render(<PaperDocumentReaderPageClient payload={needsReviewPayload} />)

    expect(screen.getByText(/May 24, 2026/)).toBeInTheDocument()
    expect(screen.queryByText(/2026年5月24日/)).not.toBeInTheDocument()
  })

  it("preserves the caller's return path in both reader states", () => {
    const { rerender } = render(<PaperDocumentReaderPage payload={compiledPayload} locale="en" backHref="/design-demo/papers?q=Agent&page=2" />)
    expect(screen.getByRole("link", { name: "Back to papers" })).toHaveAttribute("href", "/design-demo/papers?q=Agent&page=2")
    rerender(<PaperDocumentReaderPage payload={needsReviewPayload} locale="en" backHref="/design-demo/papers?q=Agent&page=2" />)
    expect(screen.getByRole("link", { name: "Back to papers" })).toHaveAttribute("href", "/design-demo/papers?q=Agent&page=2")
  })

  it("renders compiled PaperDocument blocks through the Open Reader and keeps AI summary out of the body", () => {
    render(<PaperDocumentReaderPage payload={compiledPayload} locale="en" />)

    expect(screen.getByText("Open Reader")).toBeInTheDocument()
    const article = screen.getByLabelText("Open reader paper body")
    expect(within(article).getByText(/This is real PDF paragraph text/)).toBeInTheDocument()
    expect(within(article).getByText("Figure 1")).toBeInTheDocument()
    expect(within(article).getByText("[")).toBeInTheDocument()
    expect(within(article).getByRole("button", { name: "Reference [1]" })).toBeInTheDocument()
    expect(within(article).getByRole("button", { name: "Reference [2]" })).toBeInTheDocument()
    expect(within(article).getByText("[?]")).toBeInTheDocument()
    expect(within(article).getByText("References")).toBeInTheDocument()
    expect(within(article).getByText(/Less is More/)).toBeInTheDocument()
    expect(article.querySelector(".katex")).not.toBeNull()
    expect(within(article).queryByText("Figuremodel")).not.toBeInTheDocument()
    expect(within(article).queryByText("Anything[")).not.toBeInTheDocument()
    expect(within(article).queryByText("R^Bx")).not.toBeInTheDocument()
    expect(within(article).getByText("Real figure from the PDF.")).toBeInTheDocument()
    expect(within(article).getByLabelText("y = Wx + b (1)")).toBeInTheDocument()
    expect(within(article).getByRole("table", { name: "Table 1" })).toBeInTheDocument()
    expect(within(article).getByText("0.99")).toBeInTheDocument()
    expect(within(article).getByText("Ours").closest("td")).toHaveStyle({ backgroundColor: "#d9ead3" })
    expect(within(article).getByText("0.99").closest("td")).toHaveStyle({ color: "#cc0000" })
    expect(within(article).getByText("0.99").closest("tr")?.className).toContain("ruleAfter")
    expect(within(article).queryByText("UNKNOWN")).not.toBeInTheDocument()
    expect(within(article).queryByText("unknown")).not.toBeInTheDocument()
    expect(screen.queryByText("Figure 1", { selector: "span" })).not.toBeInTheDocument()
    expect(within(article).queryByText("Asset unavailable")).not.toBeInTheDocument()
    expect(within(article).queryByText("AI generated summary must stay in the panel.")).not.toBeInTheDocument()
    expect(screen.getByRole("img", { name: "Figure 1: Real figure from the PDF." })).toHaveAttribute(
      "src",
      "/api/papers/visual-paper/assets/asset-figure-1?v=checksum",
    )
    expect(screen.getByLabelText("Reader settings")).toBeInTheDocument()
    expect(screen.getByText("Font size")).toBeInTheDocument()
    expect(screen.getByText("Text width")).toBeInTheDocument()
    expect(screen.getByText("Floating contents · drag to move")).toBeInTheDocument()
    expect(screen.getByText("Reading materials")).toBeInTheDocument()
    expect(screen.queryByLabelText("阅读设置")).not.toBeInTheDocument()
    expect(screen.queryByText("字体大小")).not.toBeInTheDocument()
    expect(screen.queryByText("悬浮目录 · 可拖动")).not.toBeInTheDocument()
  })

  it("falls back to readable math text when KaTeX cannot render a formula", () => {
    render(
      <div>
        <EquationRenderer value={String.raw`\unsupportedmacro{\mathbf{x}} + \beta`} />
        <InlineMathRenderer value={String.raw`\unsupportedmacro{\mathbf{y}}`} fallback="y" />
      </div>,
    )

    expect(screen.getByText("x + beta")).toBeInTheDocument()
    expect(screen.getByText("y")).toBeInTheDocument()
    expect(screen.queryByText(/\\unsupportedmacro/)).not.toBeInTheDocument()
  })

  it("renders the compiled outline as a numbered hierarchy", () => {
    render(<PaperDocumentReaderPage payload={compiledPayload} locale="en" />)

    const article = screen.getByLabelText("Open reader paper body")
    expect(article.querySelector("h2")?.textContent).toBe("1 Introduction")
    expect(article.querySelector("h3")?.textContent).toBe("1.1 Evaluation Details")

    const tocButtons = Array.from(document.querySelectorAll<HTMLButtonElement>("[data-level]"))
    const introductionLink = tocButtons.find((button) => button.textContent?.includes("1 Introduction"))
    const detailsLink = tocButtons.find((button) => button.textContent?.includes("1.1 Evaluation Details"))
    expect(introductionLink).toBeDefined()
    expect(detailsLink).toBeDefined()
    expect(introductionLink).toHaveAttribute("data-level", "1")
    expect(detailsLink).toHaveAttribute("data-level", "2")
  })

  it("renders textless raster table models as reader-native tables instead of image fallbacks", () => {
    render(<PaperDocumentReaderPage payload={compiledPayload} locale="en" />)

    const article = screen.getByLabelText("Open reader paper body")
    const table = within(article).getByRole("table", { name: "Table 2" })
    const tableBlock = document.querySelector<HTMLElement>('[data-block-id="block-table-raster"]')
    if (!tableBlock) throw new Error("Expected raster table block to be rendered")

    expect(table.querySelectorAll("th,td")).toHaveLength(4)
    expect(table.querySelector("th")).toHaveStyle({ backgroundColor: "#d4ebd1" })
    expect(table.querySelector("td")).toHaveStyle({ backgroundColor: "#f3cccc" })
    expect(tableBlock.querySelector("img")).toBeNull()
  })

  it("blocks article rendering for non-compiled documents", () => {
    render(<PaperDocumentReaderPage payload={needsReviewPayload} locale="en" />)

    expect(screen.getByText("Reader document is not ready yet.")).toBeInTheDocument()
    expect(screen.getByText("Some reader assets are still being prepared.")).toBeInTheDocument()
    expect(screen.queryByText("visual asset is missing")).not.toBeInTheDocument()
    expect(screen.queryByLabelText("Open reader paper body")).not.toBeInTheDocument()
    expect(screen.queryByText("This legacy section must never become body text.")).not.toBeInTheDocument()
  })

  it("shows terminal source PDF failures as failed instead of pending preparation", () => {
    render(<PaperDocumentReaderPage payload={sourcePdfFailedPayload} locale="en" />)

    expect(screen.getAllByText("Failed").length).toBeGreaterThan(0)
    expect(screen.getByText("No verified PDF")).toBeInTheDocument()
    expect(screen.queryByText("The reader document is still being prepared. You can retry later or open the PDF.")).not.toBeInTheDocument()
    expect(screen.queryByLabelText("Open reader paper body")).not.toBeInTheDocument()
  })

  it("switches from the status gate to the compiled reader after background compilation finishes", async () => {
    vi.mocked(fetchPaperDocument).mockResolvedValueOnce(compiledPayload)

    render(<PaperDocumentReaderPage payload={needsReviewPayload} locale="en" />)

    expect(screen.getByText("Reader document is not ready yet.")).toBeInTheDocument()
    expect(await screen.findByLabelText("Open reader paper body")).toBeInTheDocument()
    expect(fetchPaperDocument).toHaveBeenCalledWith(
      "visual-paper",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
  })

  it("shows the queued compile task when compile is requested", async () => {
    vi.mocked(triggerPaperCompile).mockResolvedValueOnce({
      enqueued: {
        message_id: "1-0",
        task_id: "compile-task-1",
        task_type: "papers.visual_compile",
        queue_name: "news:queue:memory",
        status: "queued",
        paper_id: "visual-paper",
      },
    })

    render(<PaperDocumentReaderPage payload={needsReviewPayload} locale="en" />)
    fireEvent.click(screen.getByRole("button", { name: "Compile" }))

    expect(await screen.findByText(/Compile task queued/)).toBeInTheDocument()
    expect(screen.getByText("compile-task-1")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Compile" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "Recompile" })).toBeDisabled()
  })

  it("shows compile request failures without changing status to queued", async () => {
    vi.mocked(triggerPaperCompile).mockRejectedValueOnce(new Error("compile service unavailable"))

    render(<PaperDocumentReaderPage payload={needsReviewPayload} locale="en" />)
    fireEvent.click(screen.getByRole("button", { name: "Compile" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("Reader compilation is temporarily unavailable. You can still open the PDF or paper detail.")
    expect(screen.queryByText("compile service unavailable")).not.toBeInTheDocument()
    expect(screen.getAllByText("Needs review").length).toBeGreaterThan(0)
    expect(screen.queryByText(/Compile task queued/)).not.toBeInTheDocument()
  })

  it("does not allow duplicate compile requests while the backend status is already queued", () => {
    render(
      <PaperDocumentReaderPage
        payload={{
          ...needsReviewPayload,
          status: {
            ...needsReviewPayload.status,
            status: "queued",
          },
        }}
        locale="en"
      />,
    )

    expect(screen.getByRole("button", { name: "Compile" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "Recompile" })).toBeDisabled()
    expect(triggerPaperCompile).not.toHaveBeenCalled()
  })

  it("allows compile requests when the backend reports not compiled", () => {
    render(
      <PaperDocumentReaderPage
        payload={{
          ...needsReviewPayload,
          status: {
            ...needsReviewPayload.status,
            status: "not_compiled",
          },
        }}
        locale="en"
      />,
    )

    expect(screen.getByRole("button", { name: "Compile" })).not.toBeDisabled()
    expect(screen.getByRole("button", { name: "Recompile" })).not.toBeDisabled()
  })

  it("opens source preview from visual blocks with source coordinates", () => {
    render(<PaperDocumentReaderPage payload={compiledPayload} locale="en" />)

    fireEvent.click(screen.getAllByTitle("Open source preview")[0])

    const preview = screen.getByRole("dialog", { name: "Source preview" })
    const image = within(preview).getByRole("img", { name: "Figure 1" })
    expect(image).toHaveAttribute(
      "src",
      expect.stringContaining("/api/papers/visual-paper/source-preview?page=1&bbox="),
    )
  })

  it("jumps from inline references and citations to their compiled targets", () => {
    const scrollIntoView = vi.fn()
    Element.prototype.scrollIntoView = scrollIntoView
    render(<PaperDocumentReaderPage payload={compiledPayload} locale="en" />)

    fireEvent.click(screen.getByText("Figure 1"))
    expect(scrollIntoView).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole("button", { name: "Reference [1]" }))
    expect(scrollIntoView).toHaveBeenCalledTimes(2)
  })

  it("uses the asset itself for source-first visual previews when PDF page crops are unavailable", () => {
    render(<PaperDocumentReaderPage payload={compiledPayload} locale="en" />)

    const tableBlock = document.querySelector<HTMLElement>('[data-block-id="block-table-1"]')
    if (!tableBlock) throw new Error("Expected table block to be rendered")
    fireEvent.click(within(tableBlock).getByTitle("Open source preview"))

    const preview = screen.getByRole("dialog", { name: "Source preview" })
    const frame = within(preview).getByTitle("Table 1")
    expect(frame).toHaveAttribute("src", "/api/papers/visual-paper/assets/asset-table-1?v=checksum-table")
  })

  it("builds reader interaction targets for visual assets", () => {
    const target = targetForPaperBlock(compiledPayload.document!.blocks[2])

    expect(target).toMatchObject({
      targetType: "figure",
      blockId: "block-figure-1",
      assetId: "asset-figure-1",
      pageNumber: 1,
      sourceBox: { x0: 100, y0: 230, x1: 500, y1: 460 },
    })
  })

  it("builds reader interaction targets for generated equation blocks", () => {
    const target = targetForPaperBlock(compiledPayload.document!.blocks[3])

    expect(target).toMatchObject({
      targetType: "equation",
      blockId: "block-equation-1",
      assetId: undefined,
      pageNumber: 1,
      sourceBox: { x0: 150, y0: 500, x1: 320, y1: 526 },
    })
  })
})

const compiledPayload: PaperDocumentResponse = {
  paper: {
    id: "visual-paper",
    slug: "visual-paper",
    title: "Visual Paper",
    abstractSnippet: "Abstract belongs in metadata.",
    authors: ["A"],
    publishedAt: "2026-05-24T00:00:00Z",
    venue: "arXiv",
    tags: [],
    taskRefs: [],
    methodRefs: [],
    paperUrl: "https://arxiv.org/abs/2605.00001",
    isPublished: true,
    aiSummary: {
      paperId: "visual-paper",
      locale: "en",
      modelRoute: "writer-primary",
      abstractHash: "hash",
      summary: "AI generated summary must stay in the panel.",
      keyInsights: ["Panel only"],
      limitations: [],
      generatedAt: "2026-05-28T00:00:00Z",
      cached: true,
    },
  },
  status: {
    paperId: "visual-paper",
    status: "compiled",
    updatedAt: "2026-05-28T00:00:00Z",
    diagnostics: [],
  },
  document: {
    paperId: "visual-paper",
    schemaVersion: "paper_document_v1",
    status: "compiled",
    title: "Visual Paper",
    compiledAt: "2026-05-28T00:00:00Z",
    sourceHash: "hash",
    paper: { id: "visual-paper", title: "Visual Paper" },
    outline: [
      { id: "block-heading-1", blockId: "block-heading-1", title: "Introduction", level: 1, pageNumber: 1, sectionNumber: "1" },
      { id: "block-heading-2", blockId: "block-heading-2", title: "Evaluation Details", level: 2, pageNumber: 1, sectionNumber: "1.1" },
    ],
    auxiliary: {
      aiSummary: "AI generated summary must stay in the panel.",
      references: [
        {
          id: "ref-wu2025less",
          key: "wu2025less",
          number: 1,
          label: "[1]",
          title: "Less is More for Subject-Driven Generation",
          authors: ["Wu, Ada", "Li, Ben"],
          year: "2025",
          venue: "arXiv",
          text: "Wu, Ada and Li, Ben. Less is More for Subject-Driven Generation. arXiv. 2025.",
        },
        {
          id: "ref-mou2025dreamo",
          key: "mou2025dreamo",
          number: 2,
          label: "[2]",
          title: "DreamO",
          authors: ["Mou, Chen"],
          year: "2025",
          venue: "CVPR",
          text: "Mou, Chen. DreamO. CVPR. 2025.",
        },
      ],
    },
    blocks: [
      {
        id: "block-heading-1",
        paperId: "visual-paper",
        type: "heading",
        text: "Introduction",
        level: 1,
        pageNumber: 1,
        source: { pageNumber: 1, bbox: { x0: 72, y0: 110, x1: 240, y1: 132 } },
        metadata: { sectionNumber: "1" },
      },
      {
        id: "block-paragraph-1",
        paperId: "visual-paper",
        type: "paragraph",
        text: String.raw`This is real PDF paragraph text. See Figure 1 and Orient Anything [?] plus UNO [1, 2]. The feature tensor is \mathcal{F} = \{F_i\}_{i=0}^{M} and F_i \in \mathbb{R}^{B \times L \times C}.`,
        pageNumber: 1,
        sectionId: "block-heading-1",
        source: { pageNumber: 1, bbox: { x0: 72, y0: 142, x1: 540, y1: 200 } },
        metadata: {
          inlineSpans: [
            { type: "text", text: "This is real PDF paragraph text. See", start: 0, end: 36 },
            { type: "ref", text: "Figure 1", label: "fig:source", refKind: "figure", targetBlockId: "block-figure-1", sectionId: "block-heading-1", display: "Figure 1", start: 37, end: 45 },
            { type: "text", text: "and Orient Anything", start: 46, end: 65 },
            { type: "citation", text: "[?]", citations: [{ key: "missing2025orient", number: null, referenceId: null, missing: true }], start: 66, end: 69 },
            { type: "text", text: "plus UNO", start: 70, end: 78 },
            {
              type: "citation",
              text: "[1, 2]",
              citations: [
                { key: "wu2025less", number: 1, referenceId: "ref-wu2025less" },
                { key: "mou2025dreamo", number: 2, referenceId: "ref-mou2025dreamo" },
              ],
              start: 79,
              end: 85,
            },
            { type: "text", text: ". The feature tensor is", start: 85, end: 108 },
            {
              type: "math",
              text: String.raw`\mathcal{F} = \{F_i\}_{i=0}^{M}`,
              latex: String.raw`\mathcal{F} = \{F_i\}_{i=0}^{M}`,
              displayMode: false,
              start: 109,
              end: 140,
            },
            { type: "text", text: "and", start: 141, end: 144 },
            {
              type: "math",
              text: String.raw`F_i \in \mathbb{R}^{B \times L \times C}`,
              latex: String.raw`F_i \in \mathbb{R}^{B \times L \times C}`,
              displayMode: false,
              start: 145,
              end: 185,
            },
            { type: "text", text: ".", start: 185, end: 186 },
          ],
        },
      },
      {
        id: "block-figure-1",
        paperId: "visual-paper",
        type: "figure",
        text: "Figure 1: Real figure from the PDF.",
        pageNumber: 1,
        sectionId: "block-heading-1",
        assetId: "asset-figure-1",
        label: "Figure 1",
        caption: "Figure 1: Real figure from the PDF.",
        source: { pageNumber: 1, bbox: { x0: 100, y0: 230, x1: 500, y1: 460 } },
      },
      {
        id: "block-equation-1",
        paperId: "visual-paper",
        type: "equation",
        text: "y = Wx + b (1)",
        pageNumber: 1,
        sectionId: "block-heading-1",
        label: "Equation 1",
        caption: "y = Wx + b (1)",
        source: { pageNumber: 1, bbox: { x0: 150, y0: 500, x1: 320, y1: 526 } },
      },
      {
        id: "block-table-1",
        paperId: "visual-paper",
        type: "table",
        text: "Table 1: Real table from the TeX source.",
        pageNumber: 1,
        sectionId: "block-heading-1",
        assetId: "asset-table-1",
        label: "Table 1",
        caption: "Table 1: Real table from the TeX source.",
        source: { pageNumber: 1, bbox: { x0: 0, y0: 0, x1: 420, y1: 150 } },
        metadata: {
          sourceProvider: "arxiv-source",
          tableModel: {
            version: 1,
            alignments: ["left", "center"],
            rows: [
              {
                rulesBefore: ["toprule"],
                cells: [
                  { text: "Method", html: "<strong>Method</strong>" },
                  { text: "Score", html: "<strong>Score</strong>" },
                ],
              },
              {
                rulesBefore: ["midrule"],
                rulesAfter: ["bottomrule"],
                rowStyle: { backgroundColor: "#f2f2f2" },
                cells: [
                  { text: "Ours", html: "Ours", classes: ["paperTableColorGray"], style: { backgroundColor: "#d9ead3" } },
                  { text: "0.99", html: "<strong>0.99</strong>", classes: ["paperTableColorBlue"], style: { color: "#cc0000" } },
                ],
              },
            ],
          },
        },
      },
      {
        id: "block-table-raster",
        paperId: "visual-paper",
        type: "table",
        text: "Table 2: Raster-only table.",
        pageNumber: 1,
        sectionId: "block-heading-1",
        assetId: "asset-table-raster",
        label: "Table 2",
        caption: "Table 2: Raster-only table.",
        source: { pageNumber: 1, bbox: { x0: 20, y0: 170, x1: 320, y1: 270 } },
        metadata: {
          sourceKind: "pdf-raster-table-model",
          tableModel: {
            version: 1,
            styleSchemaVersion: 2,
            sourceKind: "pdf-raster-table-model",
            textExtraction: "unavailable",
            alignments: ["center", "center"],
            rows: [
              {
                rowStyle: { backgroundColor: "#d4ebd1" },
                rulesBefore: ["toprule"],
                cells: [
                  { text: "", html: "", style: { backgroundColor: "#d4ebd1" } },
                  { text: "", html: "", style: { backgroundColor: "#d4ebd1" } },
                ],
              },
              {
                rulesAfter: ["bottomrule"],
                cells: [
                  { text: "", html: "", style: { backgroundColor: "#f3cccc" } },
                  { text: "", html: "" },
                ],
              },
            ],
          },
          tableHtml: "<table><tbody><tr><td></td><td></td></tr><tr><td></td><td></td></tr></tbody></table>",
        },
      },
      {
        id: "block-heading-2",
        paperId: "visual-paper",
        type: "heading",
        text: "Evaluation Details",
        level: 2,
        pageNumber: 1,
        source: { pageNumber: 1, bbox: { x0: 72, y0: 620, x1: 280, y1: 642 } },
        metadata: { sectionNumber: "1.1" },
      },
      {
        id: "block-paragraph-2",
        paperId: "visual-paper",
        type: "paragraph",
        text: "This subsection keeps the real compiled outline hierarchy.",
        pageNumber: 1,
        sectionId: "block-heading-2",
        source: { pageNumber: 1, bbox: { x0: 72, y0: 650, x1: 540, y1: 690 } },
      },
    ],
  },
  manifest: {
    paperId: "visual-paper",
    schemaVersion: "paper_document_v1",
    createdAt: "2026-05-28T00:00:00Z",
    sourceHash: "hash",
    assets: [
      {
        assetId: "asset-figure-1",
        paperId: "visual-paper",
        kind: "figure",
        fileName: "assets/asset-figure-1.png",
        mimeType: "image/png",
        width: 600,
        height: 320,
        checksum: "checksum",
        pageNumber: 1,
        label: "Figure 1",
        caption: "Figure 1: Real figure from the PDF.",
        source: { pageNumber: 1, bbox: { x0: 100, y0: 230, x1: 500, y1: 460 } },
      },
      {
        assetId: "asset-table-1",
        paperId: "visual-paper",
        kind: "table",
        fileName: "assets/asset-table-1.html",
        mimeType: "text/html; charset=utf-8",
        width: 420,
        height: 150,
        checksum: "checksum-table",
        pageNumber: 1,
        label: "Table 1",
        caption: "Table 1: Real table from the TeX source.",
        source: { pageNumber: 1, bbox: { x0: 0, y0: 0, x1: 420, y1: 150 } },
        metadata: { sourceProvider: "arxiv-source", sourceKind: "tex-table-html" },
      },
      {
        assetId: "asset-table-raster",
        paperId: "visual-paper",
        kind: "table",
        fileName: "assets/asset-table-raster.png",
        mimeType: "image/png",
        width: 300,
        height: 100,
        checksum: "checksum-raster-table",
        pageNumber: 1,
        label: "Table 2",
        caption: "Table 2: Raster-only table.",
        source: { pageNumber: 1, bbox: { x0: 20, y0: 170, x1: 320, y1: 270 } },
        metadata: { sourceKind: "pdf-raster-table-model" },
      },
    ],
  },
  ai: {
    summary: {
      paperId: "visual-paper",
      locale: "en",
      modelRoute: "writer-primary",
      abstractHash: "hash",
      summary: "AI generated summary must stay in the panel.",
      keyInsights: ["Panel only"],
      limitations: [],
      generatedAt: "2026-05-28T00:00:00Z",
      cached: true,
    },
    diagnostics: [],
  },
}

const needsReviewPayload: PaperDocumentResponse = {
  ...compiledPayload,
  status: {
    paperId: "visual-paper",
    status: "needs_review",
    updatedAt: "2026-05-28T00:00:00Z",
    diagnostics: [{ severity: "error", code: "asset_file_missing", message: "visual asset is missing" }],
  },
  document: null,
  manifest: null,
  ai: {
    diagnostics: [{ severity: "error", code: "asset_file_missing", message: "visual asset is missing" }],
  },
}

const sourcePdfFailedPayload: PaperDocumentResponse = {
  ...compiledPayload,
  status: {
    paperId: "visual-paper",
    status: "compile_failed",
    updatedAt: "2026-05-28T00:00:00Z",
    diagnostics: [{ severity: "error", code: "source_pdf_not_found", message: "HTTP Error 404: Not Found" }],
  },
  document: null,
  manifest: null,
  ai: {
    diagnostics: [{ severity: "error", code: "source_pdf_not_found", message: "HTTP Error 404: Not Found" }],
  },
}
