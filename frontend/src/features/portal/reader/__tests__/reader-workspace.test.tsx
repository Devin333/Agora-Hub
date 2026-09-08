import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { PaperReaderWorkspace } from "../paper-reader-workspace"
import type { ReaderWorkspacePayload } from "../reader-contract"
import { askPaper } from "@/lib/papers/api"
import type { Paper } from "@/lib/papers/types"

vi.mock("@/lib/papers/api", () => ({ askPaper: vi.fn(), fetchReaderMaterials: vi.fn(), recordReaderEvent: vi.fn() }))
vi.mock("@/components/papers/shared/paper-pdf-viewer", () => ({
  PaperPdfViewer: ({ pdfUrl, initialPage, onPageChange }: { pdfUrl: string; initialPage: number; onPageChange: (page: number) => void }) => <section aria-label="完整 PDF" data-source={pdfUrl}><span>第 {initialPage} 页</span><button onClick={() => onPageChange(5)}>翻到第 5 页</button></section>,
}))

const paper: Paper = { id: "reader-paper", slug: "reader-paper", title: "Research with evidence", abstractSnippet: "Published abstract only.", authors: ["Author"], publishedAt: "2026-05-21", tags: ["cs.AI"], taskRefs: [], methodRefs: [], pdfUrl: "https://arxiv.org/pdf/2605.22343", isPublished: true }
function readyWorkspace(): ReaderWorkspacePayload {
  return { paper, state: "ready", aiReady: true, reader: { paper, sections: [
    { id: "intro", paperId: paper.id, title: "Introduction", level: 1, sectionType: "unknown", textExcerpt: "Original introduction from the source.\n\nA second original paragraph." },
    { id: "method", paperId: paper.id, title: "Method", level: 1, sectionType: "unknown", textExcerpt: "Original method with verifiable evidence." },
  ], aiSummary: null, readerNotes: [], relatedPapers: [], relatedProjects: [], relatedNews: [], quality: { paperId: paper.id, pdfAvailable: true, textExtracted: true, summaryAvailable: false, implementationVerified: false, benchmarkVerified: false, evidenceCoverage: 0 } } }
}

describe("reader workspace experience", () => {
  beforeEach(() => {
    localStorage.clear()
    vi.mocked(askPaper).mockReset()
    vi.spyOn(window, "scrollTo").mockImplementation(() => undefined)
    Element.prototype.scrollIntoView = vi.fn()
  })
  afterEach(() => vi.restoreAllMocks())

  function open() {
    const rendered = render(<PaperReaderWorkspace payload={readyWorkspace()} backHref="/design-demo/papers?q=Agent&topic=cs.AI" />)
    fireEvent.click(screen.getByRole("button", { name: "章节精读", exact: true }))
    return rendered
  }

  it("keeps chapter reading primary and restores the auxiliary PDF page only when requested", () => {
    const payload = { paper, reader: null, state: "unavailable" as const, reasonCode: "research_configuration_invalid" }
    const rendered = render(<PaperReaderWorkspace payload={payload} backHref="/design-demo/papers" />)
    expect(screen.queryByRole("region", { name: "完整 PDF" })).not.toBeInTheDocument()
    expect(screen.queryByText(paper.abstractSnippet)).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "章节精读", exact: true })).toHaveAttribute("aria-pressed", "true")
    fireEvent.click(screen.getByRole("button", { name: "PDF 对照", exact: true }))
    expect(screen.getByRole("region", { name: "完整 PDF" })).toHaveAttribute("data-source", paper.pdfUrl)
    fireEvent.click(screen.getByRole("button", { name: "翻到第 5 页" }))
    rendered.unmount()
    render(<PaperReaderWorkspace payload={payload} backHref="/design-demo/papers" />)
    expect(screen.getByRole("button", { name: "章节精读", exact: true })).toHaveAttribute("aria-pressed", "true")
    fireEvent.click(screen.getByRole("button", { name: "PDF 对照", exact: true }))
    expect(screen.getByRole("region", { name: "完整 PDF" })).toHaveTextContent("第 5 页")
    fireEvent.click(screen.getByRole("button", { name: "论文概览", exact: true }))
    expect(screen.getByText(paper.abstractSnippet)).toBeVisible()
  })

  it("keeps the article separate, supports focus and navigates to a real section", async () => {
    open()
    const article = screen.getByRole("region", { name: "Open reader paper body" })
    expect(within(article).getByText("Original method with verifiable evidence.")).toBeVisible()
    expect(within(article).queryByText(paper.abstractSnippet)).not.toBeInTheDocument()
    expect(screen.getByRole("link", { name: "返回论文" })).toHaveAttribute("href", "/design-demo/papers?q=Agent&topic=cs.AI")
    fireEvent.click(screen.getByRole("button", { name: "Method", exact: true }))
    await waitFor(() => expect(Element.prototype.scrollIntoView).toHaveBeenCalled())
    fireEvent.click(screen.getByRole("button", { name: "专注", exact: true }))
    expect(screen.queryByRole("complementary", { name: "阅读助手" })).not.toBeInTheDocument()
    expect(article).toBeVisible()
    fireEvent.click(screen.getByRole("button", { name: "退出专注" }))
    expect(screen.getByRole("complementary", { name: "阅读助手" })).toBeVisible()
  })

  it("restores notes, question and type size for the same paper without leaking into another paper", async () => {
    const rendered = open()
    await waitFor(() => expect(screen.getByLabelText("向这篇论文提问")).toBeEnabled())
    fireEvent.change(screen.getByLabelText("向这篇论文提问"), { target: { value: "What evidence supports this?" } })
    fireEvent.click(screen.getByRole("button", { name: "放大正文字号" }))
    fireEvent.click(screen.getByRole("button", { name: "阅读笔记" }))
    fireEvent.change(screen.getByLabelText("我的阅读笔记"), { target: { value: "Need to check the evaluation." } })
    rendered.unmount()
    const again = open()
    fireEvent.click(screen.getByRole("button", { name: /阅读笔记/ }))
    expect(screen.getByLabelText("我的阅读笔记")).toHaveValue("Need to check the evaluation.")
    expect(screen.getByLabelText("当前正文字号")).toHaveTextContent("20")
    fireEvent.click(screen.getByRole("button", { name: "问论文", exact: true }))
    expect(screen.getByLabelText("向这篇论文提问")).toHaveValue("What evidence supports this?")
    again.unmount()
    render(<PaperReaderWorkspace payload={{ paper: { ...paper, id: "other" }, state: "unavailable", reader: null }} backHref="/design-demo/papers" />)
    fireEvent.click(screen.getByRole("button", { name: "阅读笔记" }))
    expect(screen.getByLabelText("我的阅读笔记")).toHaveValue("")
  })

  it("keeps the question on failure and allows a grounded retry with evidence", async () => {
    vi.mocked(askPaper).mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce({ paperId: paper.id, question: "What changed?", locale: "zh", answer: "The method changes the evaluation.", citations: [{ id: "evidence:1", label: "Method", sectionId: "method", sourceType: "section", textExcerpt: "Original method with verifiable evidence." }], confidence: .8, cached: false, generatedAt: "2026-09-08" })
    open()
    fireEvent.change(screen.getByLabelText("向这篇论文提问"), { target: { value: "What changed?" } })
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("问题已保留")
    expect(screen.getByLabelText("向这篇论文提问")).toHaveValue("What changed?")
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }))
    expect(await screen.findByText("The method changes the evaluation.")).toBeVisible()
    expect(askPaper).toHaveBeenLastCalledWith(paper.id, "What changed?", "zh", { signal: expect.any(AbortSignal) })
    fireEvent.click(screen.getByText("Method", { selector: "summary" }))
    expect(screen.getByRole("button", { name: "定位章节" })).toBeVisible()
  })

  it("stops an in-flight answer without losing the question", async () => {
    vi.mocked(askPaper).mockImplementation((_id, _question, _locale, init) => new Promise((_resolve, reject) => init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")))))
    open()
    fireEvent.change(screen.getByLabelText("向这篇论文提问"), { target: { value: "Explain the method" } })
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }))
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "停止回答" })))
    expect(await screen.findByRole("alert")).toHaveTextContent("已停止回答")
    expect(screen.getByLabelText("向这篇论文提问")).toHaveValue("Explain the method")
  })

  it("offers PDF and editable notes when Research is unavailable, and reports storage failures", async () => {
    render(<PaperReaderWorkspace payload={{ paper, reader: null, state: "unavailable", reasonCode: "research_configuration_invalid" }} backHref="/design-demo/papers" />)
    expect(screen.queryByRole("region", { name: "Open reader paper body" })).not.toBeInTheDocument()
    expect(screen.queryByRole("region", { name: "完整 PDF" })).not.toBeInTheDocument()
    expect(screen.getByRole("region", { name: "正文状态" })).toHaveTextContent("章节全文尚未就绪")
    expect(screen.queryByText(paper.abstractSnippet)).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText("向这篇论文提问"), { target: { value: "Keep my question" } })
    expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled()
    fireEvent.click(screen.getByRole("button", { name: "阅读笔记" }))
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("Quota", "QuotaExceededError") })
    fireEvent.change(screen.getByLabelText("我的阅读笔记"), { target: { value: "Do not lose this note" } })
    expect(screen.getByLabelText("我的阅读笔记")).toHaveValue("Do not lose this note")
    expect(screen.getByText("浏览器未能保存，请导出笔记后再离开。")).toBeVisible()
    expect(screen.getByRole("button", { name: "导出 Markdown" })).toBeEnabled()
    expect(askPaper).not.toHaveBeenCalled()
  })

  it("continues rendering source text when annotation storage is blocked", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new DOMException("Blocked", "SecurityError") })
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("Blocked", "SecurityError") })
    open()
    expect(screen.getByRole("region", { name: "Open reader paper body" })).toBeVisible()
    expect(screen.getByRole("alert")).toHaveTextContent("未能保存阅读设置或划词批注")
  })
})
