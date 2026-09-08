import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest"
import { ResearchCommandPalette } from "../research-command-palette"

type HistoryState = ReturnType<typeof import("@/lib/research/history").historyState>

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  prepareResume: vi.fn(),
  state: {
    current: {
      owner: "account-a",
      ready: true,
      status: "synced",
      message: "",
      workspace: {
        visits: [{ id: "visit-1", module: "papers", question: "Agent 证据", href: "/design-demo/papers?question=Agent", scrollY: 0, title: "Agent 检索", groupId: "group-1", isFavorite: false, createdAt: 1, updatedAt: 5, deletedAt: null }],
        groups: [{ id: "group-1", name: "课题 Alpha", createdAt: 1, updatedAt: 2 }],
        materials: [
          { id: "material-reader", groupId: "group-1", kind: "paper", title: "证据论文", url: "https://example.test/paper", readerHref: "/design-demo/papers/paper-1/read", notes: "关键引用", createdAt: 1, updatedAt: 4 },
          { id: "material-note", groupId: null, kind: "note", title: "访谈笔记", url: "", notes: "用户观察", createdAt: 1, updatedAt: 3 },
        ],
        reportDrafts: [{ id: "draft-1", groupId: "group-1", question: "研究总结", title: "研究总结", scope: "Agent 方法", notes: "", materials: [], updatedAt: 6 }],
      },
    } as HistoryState,
  },
}))

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: mocks.push }) }))
vi.mock("@/lib/research/use-research-history", () => ({ useOwnedResearchWorkspace: () => mocks.state.current }))
vi.mock("@/lib/research/history", () => ({ prepareResearchResume: mocks.prepareResume, researchResumeHref: (visit: { id: string }) => `/resume/${visit.id}` }))
vi.mock("@/lib/research/materials", () => ({ groupHref: (id: string | null) => `/design-demo/groups/${id ?? "ungrouped"}`, reportDraftHref: (draft: { id: string }) => `/reports?draft=${draft.id}` }))

function openPalette() {
  fireEvent.click(screen.getByRole("button", { name: "搜索研究和模块" }))
  return screen.getByRole("combobox", { name: "搜索研究、资料或模块" })
}

describe("research command palette", () => {
  const originalResizeObserver = globalThis.ResizeObserver
  const originalScrollIntoView = Element.prototype.scrollIntoView
  beforeAll(() => {
    globalThis.ResizeObserver = class ResizeObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    Element.prototype.scrollIntoView = vi.fn()
  })
  afterAll(() => { globalThis.ResizeObserver = originalResizeObserver; Element.prototype.scrollIntoView = originalScrollIntoView })
  beforeEach(() => {
    mocks.push.mockReset()
    mocks.prepareResume.mockReset()
    window.matchMedia = vi.fn().mockReturnValue({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })
  })
  afterEach(cleanup)

  it("opens with Ctrl/Cmd+K and searches every current-owner workspace category", () => {
    render(<ResearchCommandPalette />)
    fireEvent.keyDown(document, { key: "k", ctrlKey: true })
    const input = screen.getByRole("combobox", { name: "搜索研究、资料或模块" })
    expect(screen.getByText("Agent 检索")).toBeInTheDocument()
    fireEvent.change(input, { target: { value: "课题 Alpha" } })
    expect(screen.getByText("课题 Alpha")).toBeInTheDocument()
    fireEvent.change(input, { target: { value: "关键引用" } })
    expect(screen.getByText("证据论文")).toBeInTheDocument()
    fireEvent.change(input, { target: { value: "研究总结" } })
    expect(screen.getByText("研究总结")).toBeInTheDocument()
    fireEvent.change(input, { target: { value: "社区信号" } })
    expect(screen.getByText("社区信号")).toBeInTheDocument()
  })

  it("uses arrow keys and Enter to open the selected module", async () => {
    render(<ResearchCommandPalette />)
    const input = openPalette()
    const items = await screen.findAllByRole("option")
    await waitFor(() => expect(items[0]).toHaveAttribute("data-selected", "true"))
    fireEvent.keyDown(input, { key: "ArrowDown" })
    await waitFor(() => expect(items[1]).toHaveAttribute("data-selected", "true"))
    fireEvent.keyDown(input, { key: "Enter" })
    expect(mocks.push).toHaveBeenCalledWith("/projects")
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
  })

  it("opens history, material, group and report destinations through their owned routes", () => {
    render(<ResearchCommandPalette />)
    let input = openPalette()
    fireEvent.change(input, { target: { value: "Agent 检索" } })
    fireEvent.click(screen.getByRole("option", { name: /Agent 检索/ }))
    expect(mocks.prepareResume).toHaveBeenCalledWith(expect.objectContaining({ id: "visit-1" }))
    expect(mocks.push).toHaveBeenLastCalledWith("/resume/visit-1")

    input = openPalette()
    fireEvent.change(input, { target: { value: "证据论文" } })
    fireEvent.click(screen.getByRole("option", { name: /证据论文/ }))
    expect(mocks.push).toHaveBeenLastCalledWith("/design-demo/papers/paper-1/read")

    input = openPalette()
    fireEvent.change(input, { target: { value: "课题 Alpha" } })
    fireEvent.click(screen.getByRole("option", { name: /课题 Alpha/ }))
    expect(mocks.push).toHaveBeenLastCalledWith("/design-demo/groups/group-1")

    input = openPalette()
    fireEvent.change(input, { target: { value: "研究总结" } })
    fireEvent.click(screen.getByRole("option", { name: /研究总结/ }))
    expect(mocks.push).toHaveBeenLastCalledWith("/reports?draft=draft-1")
  })

  it("closes with Escape, restores focus and clears stale results when owner changes", async () => {
    const view = render(<><input aria-label="页面中的研究输入" /><ResearchCommandPalette /></>)
    const trigger = screen.getByRole("button", { name: "搜索研究和模块" })
    const origin = screen.getByRole("textbox", { name: "页面中的研究输入" })
    origin.focus()
    fireEvent.keyDown(document, { key: "k", metaKey: true })
    const input = screen.getByRole("combobox", { name: "搜索研究、资料或模块" })
    fireEvent.change(input, { target: { value: "证据论文" } })
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" })
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument())
    await waitFor(() => expect(origin).toHaveFocus())

    openPalette()
    mocks.state.current = { ...mocks.state.current, owner: "account-b", workspace: { visits: [], groups: [], materials: [], reportDrafts: [] } }
    view.rerender(<><input aria-label="页面中的研究输入" /><ResearchCommandPalette /></>)
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument())
    expect(openPalette()).toHaveValue("")
    expect(screen.queryByText("证据论文")).not.toBeInTheDocument()
    expect(trigger).toBeInTheDocument()
  })

  it("does not enable the global shortcut below the desktop breakpoint", () => {
    window.matchMedia = vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })
    render(<ResearchCommandPalette />)
    fireEvent.keyDown(document, { key: "k", ctrlKey: true })
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
  })
})
