import { fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { PapersDesignDemoPage } from "@/features/portal/components/papers-design-demo-page"
import { fetchPapers, fetchPaperDetail } from "@/lib/papers/api"
import type { Paper } from "@/lib/papers/types"
import { useUiStore } from "@/stores/ui-store"
import { usePaperWorkspaceStore } from "@/stores/paper-workspace-store"

const replace = vi.fn()
let query = ""

vi.mock("next/navigation", () => ({
  usePathname: () => "/design-demo/papers",
  useRouter: () => ({ replace }),
  useSearchParams: () => new URLSearchParams(query)
}))
vi.mock("@/lib/papers/api", () => ({ fetchPapers: vi.fn(), fetchPaperDetail: vi.fn(), requestPaperSummary: vi.fn() }))

const paper: Paper = {
  id: "agent-paper", slug: "agent-paper", title: "Agent evaluation",
  abstractSnippet: "Evaluation of research agents.", authors: ["Research author"],
  publishedAt: "2026-09-08T00:00:00Z", citationCount: 12,
  tags: ["cs.AI", "cs.AI"], taskRefs: [], methodRefs: [],
  pdfUrl: "https://arxiv.org/pdf/2605.00001.pdf", repoUrl: "https://github.com/owner/agent-paper",
  isPublished: true
}

describe("PapersDesignDemoPage", () => {
  beforeEach(() => {
    query = ""
    replace.mockReset()
    vi.mocked(fetchPaperDetail).mockReset().mockRejectedValue(new Error("Paper unavailable"))
    usePaperWorkspaceStore.getState().clear()
    useUiStore.setState({ locale: "zh" })
    vi.mocked(fetchPapers).mockReset().mockResolvedValue({
      source: "test", query: "", period: "all", sort: "trending", paper_count: 1,
      total_count: 1, source_count: 1, limit: 15, offset: 0, papers: [paper]
    })
  })

  it("renders public paper data and deduplicated topic counts without drafts", async () => {
    render(<PapersDesignDemoPage papers={[paper, { ...paper, id: "draft", title: "Draft", tags: ["private-topic"], isPublished: false }]} />)
    expect(await screen.findByRole("heading", { name: "Agent evaluation" })).toBeInTheDocument()
    expect(screen.queryByText("Draft")).not.toBeInTheDocument()
    expect(screen.queryByText("private-topic")).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "人工智能, cs.AI, 1 篇论文" })).toBeInTheDocument()
    expect(screen.getByRole("link", { name: "Agora AI" })).toHaveAttribute("href", "/design-demo")
    expect(screen.getByRole("link", { name: "论文研究" })).toHaveAttribute("href", "/design-demo/papers")
    await waitFor(() => expect(screen.queryByText("更新中...")).not.toBeInTheDocument())
  })

  it("keeps preview routing and active filters when submitting a search", async () => {
    query = "has=code&sort=newest&page=2"
    render(<PapersDesignDemoPage papers={[paper]} />)
    fireEvent.change(screen.getByRole("textbox", { name: "搜索论文" }), { target: { value: "  Agent  " } })
    fireEvent.click(screen.getByRole("button", { name: "搜索", exact: true }))
    const [url, options] = replace.mock.calls.at(-1)!
    const parsed = new URL(url, "http://localhost")
    expect(parsed.pathname).toBe("/design-demo/papers")
    expect(parsed.searchParams.get("q")).toBe("Agent")
    expect(parsed.searchParams.get("has")).toBe("code")
    expect(parsed.searchParams.get("sort")).toBe("newest")
    expect(parsed.searchParams.has("page")).toBe(false)
    expect(options).toEqual({ scroll: false })
    await waitFor(() => expect(fetchPapers).toHaveBeenCalled())
  })

  it("uses the existing feature, period and sort actions", async () => {
    render(<PapersDesignDemoPage papers={[paper]} />)
    fireEvent.click(screen.getByRole("button", { name: "有代码" }))
    expect(replace).toHaveBeenLastCalledWith("/design-demo/papers?has=code", { scroll: false })
    fireEvent.change(screen.getByRole("combobox", { name: "发布时间" }), { target: { value: "weekly" } })
    expect(replace).toHaveBeenLastCalledWith("/design-demo/papers?period=weekly", { scroll: false })
    fireEvent.click(screen.getByRole("button", { name: "高引用" }))
    expect(replace).toHaveBeenLastCalledWith("/design-demo/papers?sort=most_cited", { scroll: false })
    await waitFor(() => expect(fetchPapers).toHaveBeenCalled())
  })

  it("resets search and filters together", async () => {
    query = "q=agent&has=code&period=weekly&sort=newest"
    render(<PapersDesignDemoPage papers={[paper]} />)
    fireEvent.click(screen.getByRole("button", { name: "重置" }))
    expect(replace).toHaveBeenLastCalledWith("/design-demo/papers", { scroll: false })
    await waitFor(() => expect(fetchPapers).toHaveBeenCalled())
  })

  it("reflects navigation changes in the search field and topic selection", async () => {
    query = "q=Agent"
    const { rerender } = render(<PapersDesignDemoPage papers={[paper]} />)
    fireEvent.click(screen.getByRole("button", { name: "人工智能, cs.AI, 1 篇论文" }))
    expect(replace).toHaveBeenLastCalledWith("/design-demo/papers?q=Agent&topic=cs.AI", { scroll: false })
    query = "q=Agent&topic=cs.AI"
    rerender(<PapersDesignDemoPage papers={[paper]} />)
    expect(screen.getByRole("textbox", { name: "搜索论文" })).toHaveValue("Agent")
    expect(screen.getByRole("button", { name: "人工智能, cs.AI, 1 篇论文" })).toHaveAttribute("aria-pressed", "true")
    await waitFor(() => expect(fetchPapers).toHaveBeenCalledWith(expect.objectContaining({ q: "Agent", topic: "cs.AI", sort: "relevance" })))
    fireEvent.click(screen.getByRole("button", { name: "移除筛选 人工智能" }))
    expect(replace).toHaveBeenLastCalledWith("/design-demo/papers?q=Agent", { scroll: false })
    await waitFor(() => expect(screen.queryByText("更新中...")).not.toBeInTheDocument())
  })

  it("keeps preview, workspace reader and original PDF destinations functional", async () => {
    render(<PapersDesignDemoPage papers={[paper]} />)
    const row = within(await screen.findByTestId("paper-row"))
    fireEvent.click(row.getByRole("button", { name: "预览 Agent evaluation" }))
    expect(replace).toHaveBeenLastCalledWith("/design-demo/papers?paper=agent-paper", { scroll: false })
    expect(row.getByRole("link", { name: "阅读 Agent evaluation" })).toHaveAttribute("href", "/design-demo/papers/agent-paper/read?returnTo=%2Fdesign-demo%2Fpapers")
    expect(row.getByRole("link", { name: "打开论文 PDF" })).toHaveAttribute("href", paper.pdfUrl)
    await waitFor(() => expect(screen.queryByText("更新中...")).not.toBeInTheDocument())
  })

  it("shows a recoverable empty state without fabricated papers", async () => {
    query = "q=missing"
    vi.mocked(fetchPapers).mockRejectedValue(new Error("offline"))
    render(<PapersDesignDemoPage papers={[]} />)
    expect(await screen.findByRole("heading", { name: "没有找到论文" })).toBeInTheDocument()
    expect(screen.queryByTestId("paper-row")).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "清除筛选" }))
    expect(replace).toHaveBeenLastCalledWith("/design-demo/papers", { scroll: false })
    await waitFor(() => expect(fetchPapers).toHaveBeenCalled())
  })

  it("clears only time when the corpus does not cover the selected period", async () => {
    query = "q=Agent&topic=cs.AI&period=monthly"
    vi.mocked(fetchPapers).mockResolvedValue({ source: "cache", query: "Agent", period: "monthly", sort: "relevance", paper_count: 0, total_count: 0, source_count: 1, limit: 15, offset: 0, papers: [], latestPublishedAt: "2020-05-22" })
    render(<PapersDesignDemoPage papers={[paper]} />)
    expect(await screen.findByText(/所选时间段暂无数据/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "查看全部时间" }))
    expect(replace).toHaveBeenLastCalledWith("/design-demo/papers?q=Agent&topic=cs.AI", { scroll: false })
  })

  it("opens a persisted reading list and keeps the query for discovery", async () => {
    query = "q=Agent&view=reading"
    usePaperWorkspaceStore.setState({ readingList: [paper.id], later: [paper.id] })
    render(<PapersDesignDemoPage papers={[paper]} />)
    expect(screen.getByRole("heading", { name: "阅读列表" })).toBeInTheDocument()
    expect(screen.getAllByTestId("paper-row")).toHaveLength(1)
    fireEvent.click(screen.getByRole("button", { name: "发现论文" }))
    expect(replace).toHaveBeenLastCalledWith("/design-demo/papers?q=Agent", { scroll: false })
    await waitFor(() => expect(fetchPapers).toHaveBeenCalled())
  })

  it("shows five method categories with expandable pending definitions and independent group collapse", async () => {
    const methods = Array.from({ length: 7 }, (_, i) => ({ slug: `method-${i}`, name: `Method ${i}` }))
    render(<PapersDesignDemoPage papers={[paper]} taxonomy={{ methods, tasks: [] }} />)
    const group = within(screen.getByRole("region", { name: "研究方法" }))
    expect(group.getAllByRole("button", { name: /Method \d/ })).toHaveLength(5)
    expect(group.getByRole("button", { name: "Method 0, 待标注" })).toBeDisabled()
    fireEvent.click(group.getByRole("button", { name: "展开更多 研究方法" }))
    expect(group.getAllByRole("button", { name: /Method \d/ })).toHaveLength(7)
    fireEvent.click(group.getByRole("button", { name: "收起 研究方法" }))
    expect(group.getAllByRole("button", { name: /Method \d/ })).toHaveLength(5)
    fireEvent.click(group.getByRole("button", { name: "研究方法", exact: true }))
    expect(group.queryByRole("button", { name: /Method 0/ })).not.toBeInTheDocument()
    expect(group.getByRole("link", { name: "查看研究方法目录" })).toHaveAttribute("href", "/papers/methods")
    await waitFor(() => expect(fetchPapers).toHaveBeenCalled())
  })

  it("combines method and task with existing filters in API requests and local fallback", async () => {
    query = "q=Agent&topic=cs.AI&method=planning&task=agents&has=code&question=Research&page=2&paper=old"
    const annotated = { ...paper, methodRefs: [{ id: "planning", slug: "planning", name: "Planning" }], taskRefs: [{ id: "agents", slug: "agents", name: "Agents" }] }
    vi.mocked(fetchPapers).mockRejectedValue(new Error("offline"))
    render(<PapersDesignDemoPage papers={[annotated, { ...paper, id: "unclassified", title: "Unclassified Agent" }]} />)
    await waitFor(() => expect(fetchPapers).toHaveBeenCalledWith(expect.objectContaining({ q: "Agent", method: "planning", task: "agents", topic: "cs.AI" })))
    await waitFor(() => expect(screen.queryByText("更新中...")).not.toBeInTheDocument())
    expect(screen.queryByRole("heading", { name: "Unclassified Agent" })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "移除筛选 Planning" }))
    const cleared = new URL(replace.mock.calls.at(-1)![0], "http://localhost")
    expect(cleared.searchParams.get("task")).toBe("agents")
    expect(cleared.searchParams.get("topic")).toBe("cs.AI")
    expect(cleared.searchParams.get("has")).toBe("code")
    expect(cleared.searchParams.has("method")).toBe(false)
    expect(cleared.searchParams.has("page")).toBe(false)
    expect(cleared.searchParams.has("paper")).toBe(false)
    fireEvent.click(screen.getByRole("button", { name: "重置" }))
    expect(replace).toHaveBeenLastCalledWith("/design-demo/papers?question=Research", { scroll: false })
  })
})
