import { fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { PaperWorkspaceView } from "../paper-workspace-view"
import { fetchPaperDetail } from "@/lib/papers/api"
import { usePaperWorkspaceStore } from "@/stores/paper-workspace-store"
import type { Paper } from "@/lib/papers/types"

vi.mock("@/lib/papers/api", () => ({ fetchPaperDetail: vi.fn() }))
const first: Paper = { id: "one", slug: "one", title: "Agent evaluation", abstractSnippet: "Verified agent evaluation.", authors: ["Alice"], publishedAt: "2026-05-20", tags: ["cs.AI"], taskRefs: [], methodRefs: [], citationCount: 12, isPublished: true }
const second = { ...first, id: "two", slug: "two", title: "Tool learning", authors: ["Bob"], citationCount: 8 }
const props = { catalog: [first, second], locale: "zh" as const, returnTo: "/design-demo/papers?view=reading", onPreview: vi.fn(), onDiscover: vi.fn() }

describe("paper workspace", () => {
  beforeEach(() => { usePaperWorkspaceStore.getState().clear(); vi.mocked(fetchPaperDetail).mockReset() })
  it("preserves old read-later selections and removes saved membership without duplication", () => {
    usePaperWorkspaceStore.setState({ later: [first.id], readingList: [first.id] })
    render(<PaperWorkspaceView {...props} view="reading" ids={[first.id]} />)
    expect(screen.getAllByTestId("paper-row")).toHaveLength(1)
    expect(screen.getByRole("button", { name: "取消收藏 Agent evaluation" })).toHaveAttribute("aria-pressed", "true")
    fireEvent.click(screen.getByRole("button", { name: "取消收藏 Agent evaluation" }))
    expect(usePaperWorkspaceStore.getState().later).toEqual([])
    expect(usePaperWorkspaceStore.getState().readingList).toEqual([])
    const persisted = JSON.parse(localStorage.getItem("newsroom-paper-workspace")!)
    expect(persisted.state.readingList).toEqual([])
  })
  it("compares real source fields and supports removing a selection", () => {
    usePaperWorkspaceStore.setState({ compare: [first.id, second.id] })
    render(<PaperWorkspaceView {...props} view="compare" ids={[first.id, second.id]} />)
    const table = within(screen.getByRole("table"))
    expect(table.getByText("Alice")).toBeInTheDocument()
    expect(table.getByText("Bob")).toBeInTheDocument()
    expect(table.getByText("12")).toBeInTheDocument()
    expect(table.getByText("8")).toBeInTheDocument()
    fireEvent.click(table.getByRole("button", { name: "移出对比 Agent evaluation" }))
    expect(usePaperWorkspaceStore.getState().compare).toEqual([second.id])
  })
  it("retains failed IDs and resolves them through retry", async () => {
    vi.mocked(fetchPaperDetail).mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(first)
    usePaperWorkspaceStore.setState({ readingList: [first.id] })
    render(<PaperWorkspaceView {...props} catalog={[]} view="reading" ids={[first.id]} />)
    expect(await screen.findByText("部分论文暂时无法读取，选择记录已保留。")).toBeInTheDocument()
    expect(usePaperWorkspaceStore.getState().readingList).toEqual([first.id])
    fireEvent.click(screen.getByRole("button", { name: "重试" }))
    expect(await screen.findByRole("heading", { name: "Agent evaluation" })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByText("部分论文暂时无法读取，选择记录已保留。")).not.toBeInTheDocument())
  })
  it("does not expose a paper that became unpublished", async () => {
    vi.mocked(fetchPaperDetail).mockResolvedValue({ ...first, isPublished: false })
    render(<PaperWorkspaceView {...props} catalog={[]} view="reading" ids={[first.id]} />)
    expect(await screen.findByText("部分论文暂时无法读取，选择记录已保留。")).toBeInTheDocument()
    expect(screen.queryByTestId("paper-row")).not.toBeInTheDocument()
  })
})
