import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import type { ResearchReport } from "@/lib/research/desktop/contracts"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { PaperReaderPane } from "./paper-reader-pane"

const mocks = vi.hoisted(() => ({
  fetchDesktopReader: vi.fn(),
  fetchResearchReport: vi.fn(),
  saveResearchReport: vi.fn(),
  downloadResearchReport: vi.fn(),
  saveUserExport: vi.fn(),
  retryNotebookSave: vi.fn(),
  resolveNotebookConflict: vi.fn(),
  analysisEnabled: true,
  preparationEnabled: true,
  assistantUnavailable: "" as string | undefined,
  assistantMemoryEnabled: false,
  notebookUpdates: [] as Array<{ sourceRevision: string; patch: Record<string, unknown> }>,
  viewerAutoNotify: true,
  viewerLoadFails: false,
  viewerNumPages: 18,
  viewerNotificationCount: 0,
  viewerOnPageChange: undefined as ((page: number, numPages: number) => void) | undefined,
  viewerOnLoadError: undefined as (() => void) | undefined,
  notebooks: {} as Record<string, {
    note?: string
    dirty?: boolean
    saveStatus?: "synced" | "saving" | "error" | "conflict"
    saveError?: string
    readerView?: "text" | "pdf"
    pdfPage?: number
    sectionId?: string
    revisionChanged?: boolean
  }>,
  citation: {
    id: "citation-1",
    label: "citation",
    sourceType: "evidence",
    sourceRevision: "revision-1",
    valid: true,
    page: 7,
  } as {
    id: string
    label: string
    sourceType: string
    sourceRevision?: string
    valid?: boolean
    page?: number
    sectionId?: string
  },
}))

vi.mock("@/lib/research/desktop/client", () => ({
  fetchDesktopReader: mocks.fetchDesktopReader,
  fetchResearchReport: mocks.fetchResearchReport,
  saveResearchReport: mocks.saveResearchReport,
  downloadResearchReport: mocks.downloadResearchReport,
}))

vi.mock("@/features/portal/reader/use-reader-notebook", async () => {
  const React = await import("react")
  return {
    useReaderNotebook: (_paperId: string, sourceRevision: string) => {
      const configured = mocks.notebooks[sourceRevision] ?? {}
      const makeData = React.useCallback(() => ({
        sourceRevision,
        note: configured.note ?? "",
        question: "",
        fontSize: 18 as const,
        sectionId: configured.sectionId ?? "",
        resumeSection: configured.sectionId ?? "",
        pdfPage: configured.pdfPage ?? 1,
        readerView: configured.readerView ?? "text",
        updatedAt: 1,
        ready: true,
        storageError: false,
        noteOverLimit: false,
        revisionChanged: configured.revisionChanged ?? false,
        legacyAvailable: false,
        importLegacy: vi.fn(),
        saveStatus: "synced" as const,
      }), [configured.note, configured.pdfPage, configured.readerView, configured.revisionChanged, configured.sectionId, sourceRevision])
      const [data, setData] = React.useState(makeData)
      const restoredRevisionRef = React.useRef(sourceRevision)
      React.useEffect(() => {
        // Initial data is already restored; do not schedule a second stale mount write over a user click.
        if (restoredRevisionRef.current === sourceRevision) return
        restoredRevisionRef.current = sourceRevision
        setData(makeData())
      }, [makeData, sourceRevision])
      const update = React.useCallback((patch: Record<string, unknown>) => {
        mocks.notebookUpdates.push({ sourceRevision, patch })
        setData(current => ({ ...current, ...patch }))
      }, [sourceRevision])
      return { ...data, update, dirty: configured.dirty ?? false, saveStatus: configured.saveStatus ?? data.saveStatus, saveError: configured.saveError ?? "", resolving: false, retrySave: mocks.retryNotebookSave, resolveConflict: mocks.resolveNotebookConflict }
    },
  }
})

vi.mock("@/components/papers/open-reader", () => ({
  OpenReaderPage: ({ workspace }: { workspace?: { sectionRequest?: { id: string } } }) => (
    <div data-testid="open-reader" data-section={workspace?.sectionRequest?.id ?? ""}>结构化文本正文</div>
  ),
}))

vi.mock("@/components/papers/open-reader/open-reader-utils", () => ({
  buildReaderParagraphs: vi.fn(() => []),
  buildReaderToc: vi.fn(() => []),
}))

vi.mock("@/components/papers/shared/paper-pdf-viewer", async () => {
  const React = await import("react")
  return {
    PaperPdfViewer: ({
      pdfUrl,
      appearance,
      initialPage,
      onPageChange,
      onLoadError,
      fallback,
    }: {
      pdfUrl: string
      appearance?: "default" | "paper-agent"
      initialPage?: number
      onPageChange?: (page: number, numPages: number) => void
      onLoadError?: () => void
      fallback: ReactNode
    }) => {
      const actualPage = Math.min(Math.max(initialPage ?? 1, 1), mocks.viewerNumPages)
      const loadErrorRef = React.useRef(onLoadError)
      loadErrorRef.current = onLoadError
      mocks.viewerOnPageChange = onPageChange
      mocks.viewerOnLoadError = onLoadError
      React.useEffect(() => {
        if (!mocks.viewerLoadFails) return
        loadErrorRef.current?.()
      }, [pdfUrl])
      React.useEffect(() => {
        if (mocks.viewerLoadFails || !mocks.viewerAutoNotify) return
        mocks.viewerNotificationCount += 1
        onPageChange?.(actualPage, mocks.viewerNumPages)
      }, [actualPage, onPageChange, pdfUrl])
      return mocks.viewerLoadFails
        ? fallback
        : <div data-testid="pdf-viewer" data-pdf-appearance={appearance} data-pdf-url={pdfUrl} data-page={actualPage}>原始 PDF</div>
    },
  }
})

vi.mock("@/features/portal/reader/reader-assistant", () => ({
  safeReaderSourceUrl: (value?: string) => {
    if (!value) return undefined
    if (value.startsWith("/") && !value.startsWith("//")) return value
    try {
      const url = new URL(value)
      return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? value : undefined
    } catch {
      return undefined
    }
  },
  ReaderAssistant: ({ onNavigateCitation, unavailableMessage, personalMemory }: { onNavigateCitation: (citation: typeof mocks.citation) => void; unavailableMessage?: string; personalMemory?: { enabled: boolean } }) => {
    mocks.assistantUnavailable = unavailableMessage
    mocks.assistantMemoryEnabled = personalMemory?.enabled === true
    return <div><span data-testid="assistant-unavailable">{unavailableMessage ?? "ready"}</span><button type="button" onClick={() => onNavigateCitation({ ...mocks.citation })}>定位测试引用</button></div>
  },
}))

vi.mock("@/features/portal/reader/reader-analysis-control", () => ({
  ReaderAnalysisControl: ({ onRun, enabled }: { onRun: (runId: string) => void; enabled: boolean }) => {
    mocks.analysisEnabled = enabled
    return <button type="button" disabled={!enabled} onClick={() => onRun("run-2")}>刷新分析投影</button>
  },
}))

vi.mock("@/features/portal/reader/reader-preparation-control", () => ({
  ReaderPreparationControl: ({ enabled }: { enabled: boolean }) => {
    mocks.preparationEnabled = enabled
    return <button type="button" disabled={!enabled}>准备真实来源</button>
  },
}))

vi.mock("@/lib/deployment-profile", () => ({
  isHostedProfile: () => true,
}))

vi.mock("./user-export", () => ({
  saveUserExport: mocks.saveUserExport,
}))

const fingerprint = `sha256:${"a".repeat(64)}`

function privatePdf(sourceRevision: string) {
  return `/api/v1/research/desktop/papers/paper-1/source?sourceRevision=${sourceRevision}&sourceFingerprint=${fingerprint}`
}

function deferred<T>() {
  let resolve: (value: T) => void = () => { throw new Error("deferred not initialized") }
  const promise = new Promise<T>(finish => { resolve = finish })
  return { promise, resolve }
}

function readerPayload(options: {
  sourceRevision?: string
  pdfUrl?: string
  text?: boolean
  runId?: string
  outline?: string[]
} = {}) {
  const sourceRevision = options.sourceRevision ?? "revision-1"
  const text = options.text ?? true
  const paper = {
    id: "paper-1",
    slug: "paper-1",
    title: "Bounded Reader",
    authors: ["Agora"],
    abstractSnippet: "Abstract",
    publishedAt: "2026-10-01",
    venue: "Venue",
    tags: [],
    taskRefs: [],
    methodRefs: [],
    isPublished: true,
    paperUrl: "https://arxiv.org/abs/2601.01234",
    ...(options.pdfUrl ? { pdfUrl: options.pdfUrl } : {}),
  }
  return {
    paper,
    reader: text ? { paper, sections: [], aiSummary: null, readerNotes: [], relatedPapers: [], relatedProjects: [], relatedNews: [], quality: { paperId: paper.id, pdfAvailable: Boolean(options.pdfUrl), textExtracted: true, summaryAvailable: false, implementationVerified: false, benchmarkVerified: false, evidenceCoverage: 1 } } : null,
    state: text ? "ready" : "needs_review",
    reasonCode: text ? undefined : "document_quality_pending",
    visualLayer: { outline: (options.outline ?? ["methods"]).map(id => ({ id, title: id, level: 1 })) },
    sourceRevision,
    sourceFingerprint: fingerprint,
    runId: options.runId ?? "run-1",
    capabilities: text ? { ask: true, analyze: true } : { ask: false, analyze: false },
  }
}

function paneElement(online = true, memory = { useMemory: false, memoryAllowed: false }) {
  return (
    <PaperReaderPane
      paperId="paper-1"
      online={online}
      assistantOpen
      focusMode={false}
      initialTab="original"
      onAssistantClose={vi.fn()}
      onFocusChange={vi.fn()}
      onTabChange={vi.fn()}
      useMemory={memory.useMemory}
      memoryAllowed={memory.memoryAllowed}
      onUseMemoryChange={vi.fn()}
      owner="owner-1"
      onActivityChange={vi.fn()}
      onTaskStateChange={vi.fn()}
      appearance="light"
    />
  )
}

function renderPane(online = true, memory = { useMemory: false, memoryAllowed: false }) {
  return render(paneElement(online, memory))
}

describe("PaperReaderPane reader routing", () => {
  beforeEach(() => {
    mocks.fetchDesktopReader.mockReset()
    mocks.fetchResearchReport.mockReset()
    mocks.saveResearchReport.mockReset()
    mocks.downloadResearchReport.mockReset()
    mocks.saveUserExport.mockReset()
    mocks.retryNotebookSave.mockReset()
    mocks.resolveNotebookConflict.mockReset()
    mocks.analysisEnabled = true
    mocks.preparationEnabled = true
    mocks.assistantUnavailable = undefined
    mocks.assistantMemoryEnabled = false
    mocks.notebookUpdates.length = 0
    mocks.viewerAutoNotify = true
    mocks.viewerLoadFails = false
    mocks.viewerNumPages = 18
    mocks.viewerNotificationCount = 0
    mocks.viewerOnPageChange = undefined
    mocks.viewerOnLoadError = undefined
    mocks.notebooks = {
      "revision-1": { readerView: "text", pdfPage: 3 },
      "revision-2": { readerView: "text", pdfPage: 1 },
    }
    mocks.citation = {
      id: "citation-1",
      label: "citation",
      sourceType: "evidence",
      sourceRevision: "revision-1",
      valid: true,
      page: 7,
    }
  })

  afterEach(() => cleanup())

  it("keeps the existing Original tab in text mode by default and opens the exact projected PDF", async () => {
    const pdfUrl = privatePdf("revision-1")
    mocks.fetchDesktopReader.mockResolvedValue(readerPayload({ pdfUrl }))

    renderPane()

    expect(await screen.findByTestId("open-reader")).toBeInTheDocument()
    expect(screen.getByRole("tab", { name: "原文" })).toBeInTheDocument()
    const modes = screen.getByRole("group", { name: "原文阅读模式" })
    expect(modes).toHaveTextContent("文本")
    expect(modes).toHaveTextContent("PDF")
    expect(screen.getByRole("button", { name: "文本" })).toHaveAttribute("aria-pressed", "true")

    fireEvent.click(screen.getByRole("button", { name: "PDF" }))

    const viewer = await screen.findByTestId("pdf-viewer")
    expect(viewer).toHaveAttribute("data-pdf-appearance", "paper-agent")
    expect(viewer).toHaveAttribute("data-pdf-url", pdfUrl)
    expect(viewer).toHaveAttribute("data-page", "3")
    expect(mocks.notebookUpdates).toContainEqual({ sourceRevision: "revision-1", patch: { readerView: "pdf" } })
  })

  it("routes page citations only after the real viewer confirms the exact page and returns section citations to text", async () => {
    mocks.viewerAutoNotify = false
    mocks.fetchDesktopReader.mockResolvedValue(readerPayload({ pdfUrl: privatePdf("revision-1") }))
    renderPane()
    await screen.findByTestId("open-reader")

    fireEvent.click(screen.getByRole("button", { name: "定位测试引用" }))
    expect(await screen.findByTestId("pdf-viewer")).toHaveAttribute("data-page", "7")
    expect(screen.getByRole("status")).toHaveTextContent("正在打开 PDF 第 7 页")
    expect(screen.getByRole("status")).not.toHaveTextContent("已定位到 PDF 第 7 页")
    expect(mocks.notebookUpdates).toContainEqual({ sourceRevision: "revision-1", patch: { readerView: "pdf" } })
    expect(mocks.notebookUpdates).not.toContainEqual({ sourceRevision: "revision-1", patch: { pdfPage: 7 } })

    await act(async () => mocks.viewerOnPageChange?.(7, 18))
    expect(screen.getByRole("status")).toHaveTextContent("已定位到 PDF 第 7 页")
    expect(mocks.notebookUpdates).toContainEqual({ sourceRevision: "revision-1", patch: { pdfPage: 7 } })

    mocks.citation = {
      id: "citation-2",
      label: "Methods",
      sourceType: "section",
      sourceRevision: "revision-1",
      valid: true,
      sectionId: "methods",
    }
    fireEvent.click(screen.getByRole("button", { name: "定位测试引用" }))

    expect(await screen.findByTestId("open-reader")).toHaveAttribute("data-section", "methods")
    expect(mocks.notebookUpdates).toContainEqual({
      sourceRevision: "revision-1",
      patch: { sectionId: "methods", readerView: "text" },
    })
  })

  it("does not synthesize PDF mode from a public paper URL", async () => {
    mocks.fetchDesktopReader.mockResolvedValue(readerPayload())
    renderPane()

    await screen.findByTestId("open-reader")
    expect(screen.queryByRole("button", { name: "PDF" })).not.toBeInTheDocument()
    expect(screen.queryByTestId("pdf-viewer")).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "定位测试引用" }))
    expect(await screen.findByRole("status")).toHaveTextContent("没有当前修订可核对的精确位置")
    expect(screen.queryByTestId("pdf-viewer")).not.toBeInTheDocument()
  })

  it("falls back to the exact PDF when text is unavailable without pretending text is selectable", async () => {
    const pdfUrl = privatePdf("revision-1")
    mocks.fetchDesktopReader.mockResolvedValue(readerPayload({ pdfUrl, text: false }))
    renderPane()

    expect(await screen.findByTestId("pdf-viewer")).toHaveAttribute("data-pdf-url", pdfUrl)
    expect(screen.getByRole("button", { name: "文本" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "PDF" })).toHaveAttribute("aria-pressed", "true")
  })

  it("rejects a citation beyond an actual 18-page document instead of claiming the clamped page", async () => {
    mocks.citation.page = 19
    mocks.fetchDesktopReader.mockResolvedValue(readerPayload({ pdfUrl: privatePdf("revision-1") }))
    renderPane()
    await screen.findByTestId("open-reader")

    fireEvent.click(screen.getByRole("button", { name: "定位测试引用" }))

    expect(await screen.findByTestId("pdf-viewer")).toHaveAttribute("data-page", "18")
    expect(await screen.findByRole("status")).toHaveTextContent("引用页 19 超出当前 PDF 的 18 页范围，定位已失效")
    expect(screen.getByRole("status")).not.toHaveTextContent("已定位到 PDF 第 19 页")
    expect(mocks.notebookUpdates).not.toContainEqual({ sourceRevision: "revision-1", patch: { pdfPage: 19 } })
    expect(mocks.notebookUpdates).toContainEqual({ sourceRevision: "revision-1", patch: { pdfPage: 18 } })
  })

  it("keeps load failure honest and never retains a pending citation success", async () => {
    mocks.viewerLoadFails = true
    mocks.fetchDesktopReader.mockResolvedValue(readerPayload({ pdfUrl: privatePdf("revision-1") }))
    renderPane()
    await screen.findByTestId("open-reader")

    fireEvent.click(screen.getByRole("button", { name: "定位测试引用" }))

    expect(await screen.findByText("原文尚未就绪")).toBeInTheDocument()
    expect(screen.getByRole("status")).toHaveTextContent("PDF 加载失败，未能定位到第 7 页")
    expect(screen.getByRole("status")).not.toHaveTextContent("已定位到 PDF 第 7 页")
    expect(mocks.notebookUpdates).not.toContainEqual({ sourceRevision: "revision-1", patch: { pdfPage: 7 } })
  })

  it("keeps the page callback stable across parent rerenders and does not duplicate workspace writes", async () => {
    mocks.notebooks["revision-1"] = { readerView: "pdf", pdfPage: 3 }
    mocks.fetchDesktopReader.mockResolvedValue(readerPayload({ pdfUrl: privatePdf("revision-1") }))
    const view = renderPane()

    await screen.findByTestId("pdf-viewer")
    await waitFor(() => expect(mocks.viewerNotificationCount).toBe(1))
    expect(mocks.notebookUpdates.filter(item => "pdfPage" in item.patch)).toHaveLength(0)

    view.rerender(paneElement())
    await act(async () => undefined)

    expect(mocks.viewerNotificationCount).toBe(1)
    expect(mocks.notebookUpdates.filter(item => "pdfPage" in item.patch)).toHaveLength(0)
  })

  it("does not reuse the previous revision view or section locator after refresh", async () => {
    mocks.notebooks["revision-1"] = { readerView: "pdf", pdfPage: 9, sectionId: "methods" }
    mocks.fetchDesktopReader
      .mockResolvedValueOnce(readerPayload({ pdfUrl: privatePdf("revision-1"), outline: ["methods"] }))
      .mockResolvedValueOnce(readerPayload({ sourceRevision: "revision-2", pdfUrl: privatePdf("revision-2"), runId: "run-2", outline: ["results"] }))
    renderPane()

    expect(await screen.findByTestId("pdf-viewer")).toHaveAttribute("data-page", "9")
    fireEvent.click(screen.getByRole("tab", { name: "概览" }))
    fireEvent.click(await screen.findByRole("button", { name: "刷新分析投影" }))
    await waitFor(() => expect(mocks.fetchDesktopReader).toHaveBeenCalledTimes(2))
    fireEvent.click(screen.getByRole("tab", { name: "原文" }))

    await waitFor(() => expect(screen.getByTestId("open-reader")).toBeInTheDocument())
    expect(screen.getByTestId("open-reader")).toHaveAttribute("data-section", "")
    expect(screen.queryByTestId("pdf-viewer")).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "文本" })).toHaveAttribute("aria-pressed", "true")
  })

  it("does not enable task memory when this task has no current policy authorization", async () => {
    mocks.fetchDesktopReader.mockResolvedValue(readerPayload())
    renderPane(true, { useMemory: true, memoryAllowed: false })

    await screen.findByTestId("open-reader")
    expect(mocks.assistantMemoryEnabled).toBe(false)
  })

  it("keeps local reading available offline while disabling new prepare, analyze and ask work", async () => {
    mocks.fetchDesktopReader.mockResolvedValue(readerPayload())
    const first = renderPane(false)

    await screen.findByTestId("open-reader")
    expect(screen.getByText(/当前离线：本地原文、历史成果和笔记仍可使用/)).toBeVisible()
    expect(screen.getByTestId("assistant-unavailable")).toHaveTextContent("不会排队或自动补发")
    fireEvent.click(screen.getByRole("tab", { name: "概览" }))
    expect(screen.getByRole("button", { name: "刷新分析投影" })).toBeDisabled()
    expect(mocks.analysisEnabled).toBe(false)

    first.unmount()
    mocks.fetchDesktopReader.mockResolvedValue({ ...readerPayload(), capabilities: undefined })
    renderPane(false)
    await screen.findByTestId("open-reader")
    fireEvent.click(screen.getByRole("tab", { name: "概览" }))
    expect(await screen.findByRole("button", { name: "准备真实来源" })).toBeDisabled()
    expect(mocks.preparationEnabled).toBe(false)
  })

  it("loads the typed report before exposing gate, save or native export boundaries", async () => {
    const readerRequest = deferred<ReturnType<typeof readerPayload>>()
    const reportRequest = deferred<ResearchReport>()
    const typedReport: ResearchReport = {
      schema: "newsroom.research-report/v1",
      runId: "run-1",
      paperId: "paper-1",
      sourceRevision: "revision-1",
      status: "rejected",
      disposition: "quality_gate_rejected",
      analysis: null,
      quality: null,
      citations: [],
      artifactRefs: [],
      traceRef: null,
      diagnostics: { artifactStatus: "missing", artifactReason: "candidate was not promoted" },
      eligibility: { accepted: false, qualityPassed: false, acceptedExport: false },
    }
    mocks.fetchDesktopReader.mockReturnValueOnce(readerRequest.promise)
    mocks.fetchResearchReport.mockReturnValueOnce(reportRequest.promise)
    renderPane()

    expect(screen.getByRole("heading", { name: "正在读取论文" })).toBeVisible()
    expect(mocks.fetchResearchReport).not.toHaveBeenCalled()
    // Settle the reader response, LoadedReader mount and initial-tab effects
    // before interacting; open-reader's first render alone is not that boundary.
    await act(async () => {
      readerRequest.resolve(readerPayload())
      await readerRequest.promise
    })
    expect(await screen.findByTestId("open-reader")).toBeVisible()
    const detailsTab = screen.getByRole("tab", { name: "详情" })
    await act(async () => { fireEvent.click(detailsTab) })
    await waitFor(() => {
      expect(detailsTab).toHaveAttribute("aria-selected", "true")
      expect(screen.getByRole("button", { name: "保存合格报告" })).toBeDisabled()
      expect(screen.getByRole("button", { name: "Markdown" })).toBeDisabled()
      expect(screen.getByRole("button", { name: "结构化 JSON" })).toBeDisabled()
    })
    expect(mocks.fetchResearchReport).not.toHaveBeenCalled()
    expect(screen.queryByText("质量门未通过")).not.toBeInTheDocument()
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "读取真实报告" })) })
    expect(mocks.fetchResearchReport).toHaveBeenCalledTimes(1)
    expect(mocks.fetchResearchReport).toHaveBeenCalledWith("run-1", "revision-1")
    expect(screen.getByRole("button", { name: "保存合格报告" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "Markdown" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "结构化 JSON" })).toBeDisabled()
    expect(screen.queryByText("质量门未通过")).not.toBeInTheDocument()

    await act(async () => {
      reportRequest.resolve(typedReport)
      await reportRequest.promise
    })
    expect(await screen.findByText("质量门未通过")).toBeVisible()
    expect(screen.getByText("候选/草稿，不可保存为合格报告")).toBeVisible()
    expect(screen.getByText(/草稿\/被拒候选/)).toBeVisible()
    expect(screen.getByRole("button", { name: "保存合格报告" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "Markdown" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "结构化 JSON" })).toBeDisabled()
    expect(mocks.saveResearchReport).not.toHaveBeenCalled()
    expect(mocks.downloadResearchReport).not.toHaveBeenCalled()
    expect(mocks.saveUserExport).not.toHaveBeenCalled()
  })


  it("rejects a report receipt from a different paper identity", async () => {
    mocks.fetchDesktopReader.mockResolvedValue(readerPayload())
    mocks.fetchResearchReport.mockResolvedValue({
      schema: "newsroom.research-report/v1", runId: "run-1", paperId: "paper-other", sourceRevision: "revision-1",
      status: "completed", disposition: "accepted", analysis: {}, quality: { passed: true }, citations: [], artifactRefs: [], traceRef: null,
      diagnostics: { artifactStatus: "valid", artifactReason: "" },
      eligibility: { accepted: true, qualityPassed: true, acceptedExport: true },
    })
    renderPane()

    await screen.findByTestId("open-reader")
    fireEvent.click(screen.getByRole("tab", { name: "详情" }))
    fireEvent.click(screen.getByRole("button", { name: "读取真实报告" }))

    expect(await screen.findByText(/报告回执与当前 paper\/run\/source revision 不一致/)).toBeVisible()
    expect(screen.getByRole("button", { name: "保存合格报告" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "Markdown" })).toBeDisabled()
    expect(mocks.saveResearchReport).not.toHaveBeenCalled()
    expect(mocks.downloadResearchReport).not.toHaveBeenCalled()
  })

  it("forwards only a loaded accepted report payload to native Markdown export", async () => {
    mocks.fetchDesktopReader.mockResolvedValue(readerPayload())
    mocks.fetchResearchReport.mockResolvedValue({
      schema: "newsroom.research-report/v1", runId: "run-1", paperId: "paper-1", sourceRevision: "revision-1",
      status: "completed", disposition: "accepted", analysis: {}, quality: { passed: true }, citations: [], artifactRefs: [], traceRef: null,
      diagnostics: { artifactStatus: "valid", artifactReason: "", reportId: "report-1" },
      eligibility: { accepted: true, qualityPassed: true, acceptedExport: true },
    })
    mocks.downloadResearchReport
      .mockResolvedValueOnce({ filename: "accepted-report.md", blob: new Blob(["# Accepted report\n\nSource-backed content"]) })
      .mockResolvedValueOnce({ filename: "accepted-report.json", blob: new Blob(['{"schema":"newsroom.research-report/v1"}']) })
    mocks.saveUserExport.mockResolvedValue({ ok: true, code: "saved", fileName: "accepted-report.md", message: "已保存" })
    renderPane()

    await screen.findByTestId("open-reader")
    fireEvent.click(screen.getByRole("tab", { name: "详情" }))
    fireEvent.click(screen.getByRole("button", { name: "读取真实报告" }))
    await waitFor(() => expect(screen.getByRole("button", { name: "Markdown" })).toBeEnabled())
    expect(screen.getByText("质量门通过")).toBeVisible()
    expect(screen.getByText("已接受，可保存并原生导出")).toBeVisible()
    fireEvent.click(screen.getByRole("button", { name: "Markdown" }))

    await waitFor(() => expect(mocks.saveUserExport).toHaveBeenCalledWith({
      suggestedName: "accepted-report", format: "md", content: "# Accepted report\n\nSource-backed content",
    }))
    expect(mocks.downloadResearchReport).toHaveBeenCalledWith("run-1", "revision-1", "markdown")

    await waitFor(() => expect(screen.getByRole("button", { name: "结构化 JSON" })).toBeEnabled())
    fireEvent.click(screen.getByRole("button", { name: "结构化 JSON" }))
    await waitFor(() => expect(mocks.saveUserExport).toHaveBeenLastCalledWith({
      suggestedName: "accepted-report", format: "json", content: '{"schema":"newsroom.research-report/v1"}',
    }))
    expect(mocks.downloadResearchReport).toHaveBeenLastCalledWith("run-1", "revision-1", "json")
  })

  it("retains the visible failed draft and exposes an explicit save retry", async () => {
    mocks.fetchDesktopReader.mockResolvedValue(readerPayload())
    mocks.notebooks["revision-1"] = { note: "retained failed draft", dirty: true, saveStatus: "error", saveError: "保存失败；草稿保留" }
    renderPane()
    await screen.findByTestId("open-reader")
    fireEvent.click(screen.getByRole("tab", { name: "笔记" }))
    expect(screen.getByRole("textbox")).toHaveValue("retained failed draft")
    expect(screen.getByRole("alert")).toHaveTextContent("保存失败；草稿保留")
    fireEvent.click(screen.getByRole("button", { name: "重试保存草稿" }))
    expect(mocks.retryNotebookSave).toHaveBeenCalledOnce()
    expect(mocks.resolveNotebookConflict).not.toHaveBeenCalled()
  })

  it("requires explicit conflict choices and confirms before discarding a notebook draft", async () => {
    mocks.fetchDesktopReader.mockResolvedValue(readerPayload())
    mocks.notebooks["revision-1"] = { note: "conflicting draft", dirty: true, saveStatus: "conflict" }
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true)
    try {
      renderPane()
      await screen.findByTestId("open-reader")
      fireEvent.click(screen.getByRole("tab", { name: "笔记" }))
      expect(mocks.resolveNotebookConflict).not.toHaveBeenCalled()
      fireEvent.click(screen.getByRole("button", { name: "保留本页草稿并核对最新 revision" }))
      expect(mocks.resolveNotebookConflict).toHaveBeenLastCalledWith("keep-draft")
      mocks.resolveNotebookConflict.mockClear()
      fireEvent.click(screen.getByRole("button", { name: "采用服务端版本" }))
      expect(mocks.resolveNotebookConflict).not.toHaveBeenCalled()
      expect(screen.getByRole("textbox")).toHaveValue("conflicting draft")
      fireEvent.click(screen.getByRole("button", { name: "采用服务端版本" }))
      expect(mocks.resolveNotebookConflict).toHaveBeenLastCalledWith("use-server")
    } finally { confirm.mockRestore() }
  })

})
