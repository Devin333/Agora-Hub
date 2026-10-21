import { StrictMode } from "react"
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { PREVIEW_PAPERS, TOPICS } from "./data"
import { PaperAgentLab } from "./paper-agent-lab"
import { DEFAULT_PREVIEW_STATE, parsePreviewState } from "./preview-state"
import type { PreviewState } from "./types"

const byId = new Map(PREVIEW_PAPERS.map((paper) => [paper.id, paper]))
const memgpt = byId.get("memgpt")!
const generativeAgents = byId.get("generative-agents")!
const rag = byId.get("rag")!
const attention = byId.get("attention")!

function setLocation(search = "") {
  window.history.replaceState({}, "", `/paper-agent-lab${search}`)
}

function renderLab(initialState: PreviewState = DEFAULT_PREVIEW_STATE) {
  return render(<PaperAgentLab initialState={initialState} />)
}

function selectPaper(paperId: (typeof PREVIEW_PAPERS)[number]["id"]) {
  const paper = byId.get(paperId)!
  fireEvent.click(screen.getByRole("button", { name: `选择论文：${paper.title}` }))
}

function openNotes() {
  fireEvent.click(screen.getByRole("tab", { name: "笔记" }))
  return screen.getByRole("textbox", { name: "本篇笔记" })
}

describe("PaperAgentLab", () => {
  beforeEach(() => {
    setLocation()
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    setLocation()
  })

  it("renders one fixed desktop paper workspace from the shared real-paper catalogue", () => {
    renderLab()

    const root = screen.getByTestId("paper-agent-lab")
    expect(root).toHaveAttribute("data-theme", "light")
    expect(screen.getByRole("complementary", { name: "论文工作台导航" })).toBeVisible()
    expect(screen.getByRole("region", { name: "论文列表" })).toBeVisible()
    expect(screen.getByRole("region", { name: `${memgpt.title} 阅读区` })).toBeVisible()
    expect(screen.getAllByRole("button", { name: /^选择论文：/ })).toHaveLength(PREVIEW_PAPERS.length)
    expect(screen.getByRole("heading", { name: memgpt.title })).toBeVisible()
    expect(TOPICS.map((topic) => screen.getByRole("button", { name: topic.label }))).toHaveLength(3)
    expect(screen.queryByText(/十种设计|设计风格|选择风格/)).not.toBeInTheDocument()
    expect(within(screen.getByRole("complementary", { name: "论文工作台导航" })).getByText("Agora")).toBeVisible()
    expect(screen.queryByText("Papers / 论文工作台")).not.toBeInTheDocument()
    expect(screen.queryByText("A", { selector: "span" })).not.toBeInTheDocument()
  })

  it.each([
    ["title", "Retrieval-Augmented", rag.id],
    ["author", "Patrick Lewis", rag.id],
    ["tag", "Transformer", attention.id],
  ])("searches by %s using the shared paper data", (_field, query, expectedId) => {
    renderLab()

    const search = screen.getByPlaceholderText("搜索标题、作者或标签")
    fireEvent.change(search, { target: { value: query } })

    expect(screen.getAllByRole("button", { name: /^选择论文：/ })).toHaveLength(1)
    expect(screen.getByRole("button", { name: `选择论文：${byId.get(expectedId)!.title}` })).toBeVisible()
  })

  it("shows an honest empty search state without inventing retrieval results", () => {
    renderLab()

    fireEvent.change(screen.getByPlaceholderText("搜索标题、作者或标签"), {
      target: { value: "不存在的量子香蕉论文" },
    })

    expect(screen.getByText("此视图暂无论文")).toBeVisible()
    expect(screen.getByText(/清除搜索条件|更短的关键词/)).toBeVisible()
    expect(screen.queryByRole("button", { name: /^选择论文：/ })).not.toBeInTheDocument()
    expect(screen.queryByRole("region", { name: `${memgpt.title} 阅读区` })).not.toBeInTheDocument()
    expect(screen.queryByText(/已找到.*篇|联网检索|模型推荐/)).not.toBeInTheDocument()
  })

  it.each([
    ["Control", { ctrlKey: true }],
    ["Meta", { metaKey: true }],
  ] as const)("focuses search with %s+K without changing page state", async (_modifier, modifier) => {
    renderLab()

    const search = screen.getByPlaceholderText("搜索标题、作者或标签")
    fireEvent.keyDown(window, { key: "k", ...modifier })

    await waitFor(() => expect(search).toHaveFocus())
    expect(window.location.pathname).toBe("/paper-agent-lab")
  })

  it("filters by topic, sorts visible papers, and selects a paper from the result", async () => {
    renderLab()

    fireEvent.click(screen.getByRole("button", { name: "检索与生成" }))
    expect(screen.getAllByRole("button", { name: /^选择论文：/ })).toHaveLength(
      PREVIEW_PAPERS.filter((paper) => paper.topic === "retrieval").length,
    )
    expect(screen.getByRole("button", { name: `选择论文：${rag.title}` })).toBeVisible()
    expect(screen.queryByRole("button", { name: `选择论文：${attention.title}` })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: /^全部论文/ }))
    fireEvent.change(screen.getByDisplayValue("默认排序"), { target: { value: "title" } })
    const firstPaper = screen.getAllByRole("listitem")[0]
    expect(within(firstPaper).getByText(attention.title)).toBeVisible()

    selectPaper(attention.id)
    expect(screen.getByRole("heading", { name: attention.title })).toBeVisible()
    await waitFor(() => expect(window.location.search).toContain("paper=attention"))
  })

  it("keeps saved state in page memory and clears a now-empty saved selection", () => {
    renderLab()

    fireEvent.click(within(screen.getByRole("region", { name: `${memgpt.title} 阅读区` }))
      .getByRole("button", { name: `收藏：${memgpt.title}` }))
    fireEvent.click(screen.getByRole("button", { name: /^我的收藏/ }))

    expect(screen.getByRole("button", { name: `选择论文：${memgpt.title}` })).toBeVisible()
    fireEvent.click(within(screen.getByRole("region", { name: `${memgpt.title} 阅读区` }))
      .getByRole("button", { name: `取消收藏：${memgpt.title}` }))

    expect(screen.getByText("此视图暂无论文")).toBeVisible()
    expect(screen.queryByRole("button", { name: `选择论文：${memgpt.title}` })).not.toBeInTheDocument()
    expect(screen.queryByRole("region", { name: `${memgpt.title} 阅读区` })).not.toBeInTheDocument()
  })

  it("updates unread filtering from real read state and can restore the paper", () => {
    renderLab()

    fireEvent.click(screen.getByRole("button", { name: /^未读/ }))
    expect(screen.getAllByRole("button", { name: /^选择论文：/ })).toHaveLength(PREVIEW_PAPERS.length)

    fireEvent.click(screen.getByRole("button", { name: "标为已读" }))
    expect(screen.queryByRole("button", { name: `选择论文：${memgpt.title}` })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: /^全部论文/ }))
    selectPaper(memgpt.id)
    fireEvent.click(screen.getByRole("button", { name: "标为未读" }))
    fireEvent.click(screen.getByRole("button", { name: /^未读/ }))
    expect(screen.getByRole("button", { name: `选择论文：${memgpt.title}` })).toBeVisible()
  })

  it("isolates in-memory notes by paper and restores each note when returning", () => {
    renderLab()

    const memgptNote = "关注分层记忆与上下文调度。"
    fireEvent.change(openNotes(), { target: { value: memgptNote } })

    selectPaper(generativeAgents.id)
    openNotes()
    expect(screen.getByRole("textbox", { name: "本篇笔记" })).toHaveValue("")
    fireEvent.change(screen.getByRole("textbox", { name: "本篇笔记" }), {
      target: { value: "关注记忆流、反思与计划。" },
    })

    selectPaper(memgpt.id)
    openNotes()
    expect(screen.getByRole("textbox", { name: "本篇笔记" })).toHaveValue(memgptNote)
  })

  it("canonicalizes invalid initial URL state and follows popstate navigation", async () => {
    setLocation("?style=gallery&mode=app&theme=sepia&paper=missing&view=recent&tab=full&focus=1&assistant=yes")
    renderLab(parsePreviewState(window.location.search))

    await waitFor(() => expect(window.location.pathname + window.location.search).toBe(
      "/paper-agent-lab?theme=light&paper=memgpt&view=all&tab=overview&focus=false&assistant=false",
    ))
    expect(screen.getByTestId("paper-agent-lab")).toHaveAttribute("data-theme", "light")
    expect(screen.getByRole("heading", { name: memgpt.title })).toBeVisible()

    act(() => {
      window.history.pushState({}, "", "/paper-agent-lab?theme=dark&paper=rag&view=all&tab=details&focus=false&assistant=true")
      window.dispatchEvent(new PopStateEvent("popstate"))
    })

    await waitFor(() => expect(screen.getByTestId("paper-agent-lab")).toHaveAttribute("data-theme", "dark"))
    expect(screen.getByRole("tab", { name: "详情" })).toHaveAttribute("aria-selected", "true")
    expect(screen.getByText(rag.arxivId)).toBeVisible()
    expect(screen.getByRole("button", { name: "关闭论文助手" })).toBeVisible()
  })

  it("performs exactly one history push for one interaction under StrictMode", async () => {
    setLocation("?theme=light&paper=memgpt&view=all&tab=overview&focus=false&assistant=false")
    const pushState = vi.spyOn(window.history, "pushState")

    render(
      <StrictMode>
        <PaperAgentLab initialState={DEFAULT_PREVIEW_STATE} />
      </StrictMode>,
    )
    await waitFor(() => expect(screen.getByTestId("paper-agent-lab")).toBeVisible())
    pushState.mockClear()

    fireEvent.click(screen.getByRole("button", { name: "使用黑色主题" }))

    expect(pushState).toHaveBeenCalledTimes(1)
    expect(window.location.search).toContain("theme=dark")
  })
  it("writes theme, paper, view, tab, focus and assistant changes to the canonical URL", async () => {
    renderLab()

    fireEvent.click(screen.getByRole("button", { name: "使用黑色主题" }))
    selectPaper(rag.id)
    fireEvent.click(screen.getByRole("button", { name: /^未读/ }))
    fireEvent.click(screen.getByRole("tab", { name: "笔记" }))
    fireEvent.click(screen.getByRole("button", { name: "专注阅读" }))
    await waitFor(() => expect(window.location.search).toContain("focus=true"))
    expect(screen.getByRole("button", { name: "退出专注阅读" })).toBeVisible()

    fireEvent.click(screen.getByRole("button", { name: "打开论文助手" }))
    await waitFor(() => expect(window.location.pathname + window.location.search).toBe(
      "/paper-agent-lab?theme=dark&paper=rag&view=unread&tab=notes&focus=false&assistant=true",
    ))
    expect(screen.getByTestId("paper-agent-lab")).toHaveAttribute("data-theme", "dark")
    expect(screen.getByRole("button", { name: "关闭论文助手" })).toBeVisible()
  })

  it("keeps the desktop assistant non-modal so the reader remains operable", () => {
    renderLab()

    fireEvent.click(screen.getByRole("button", { name: "打开论文助手" }))

    const assistantShell = document.querySelector('[role="complementary"][aria-label="论文助手"]')
    expect(assistantShell).not.toBeNull()
    expect(assistantShell).not.toHaveAttribute("aria-modal")
    expect(screen.getByLabelText("论文列表").closest("[inert]")).toBeNull()

    fireEvent.click(screen.getByRole("tab", { name: "详情" }))
    expect(screen.getByRole("tab", { name: "详情" })).toHaveAttribute("aria-selected", "true")
  })

  it("makes the narrow assistant modal and inerts the background", async () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      matches: true,
      media: "(max-width: 1100px)",
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })))
    renderLab()

    fireEvent.click(screen.getByRole("button", { name: "打开论文助手" }))

    const dialog = await screen.findByRole("dialog", { name: "论文助手" })
    expect(dialog).toHaveAttribute("aria-modal", "true")
    expect(screen.getByLabelText("论文列表").closest("[inert]")).not.toBeNull()
    await waitFor(() => expect(screen.getByRole("button", { name: "关闭论文助手" })).toHaveFocus())
  })

  it("submits the real question without a fabricated answer and does not leak it across papers", () => {
    renderLab()

    fireEvent.click(screen.getByRole("button", { name: "打开论文助手" }))
    const input = screen.getByRole("textbox", { name: "向论文助手提问" })
    fireEvent.change(input, { target: { value: "这篇论文如何管理上下文边界？" } })
    fireEvent.click(screen.getByRole("button", { name: "提交预览问题" }))

    expect(screen.getByText("这篇论文如何管理上下文边界？")).toBeVisible()
    expect(screen.getAllByText(/不会调用|未调用|未发送/).length).toBeGreaterThan(0)
    expect(screen.queryByText(/模型回答|AI 回答|生成答案/)).not.toBeInTheDocument()

    selectPaper(generativeAgents.id)
    expect(screen.queryByText("这篇论文如何管理上下文边界？")).not.toBeInTheDocument()
    expect(screen.getByRole("textbox", { name: "向论文助手提问" })).toHaveValue("")

    selectPaper(memgpt.id)
    expect(screen.getByText("这篇论文如何管理上下文边界？")).toBeVisible()
  })
})

