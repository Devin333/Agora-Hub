import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ResearchMaterialContext } from "./research-material-context"
import { acceptAccountHistory, readResearchWorkspace, saveResearchGroup, saveResearchMaterial, selectHistoryOwner } from "@/lib/research/history"

const navigation = vi.hoisted(() => ({ query: "", push: vi.fn(), replace: vi.fn() }))
vi.mock("next/navigation", () => ({ usePathname: () => "/design-demo/papers", useSearchParams: () => new URLSearchParams(navigation.query), useRouter: () => navigation }))

describe("selected research sources at a destination", () => {
  beforeEach(() => {
    localStorage.clear(); sessionStorage.clear(); selectHistoryOwner(null)
    navigation.push.mockReset(); navigation.replace.mockReset()
    navigation.query = "question=Agent&researchSession=study&researchGroup=group-a&material=paper-a&material=unknown"
    saveResearchGroup("Agent", "group-a")
    saveResearchMaterial({ id: "paper-a", groupId: "group-a", kind: "paper", title: "Real source", url: "https://arxiv.org/abs/2605.22343", notes: "Methods to examine", createdAt: 1, updatedAt: 2 })
  })
  afterEach(() => { cleanup(); selectHistoryOwner(null) })

  it("shows only owned references, preserves provenance and prepares a report without copying links", () => {
    render(<ResearchMaterialContext />)
    expect(screen.getByRole("status")).toHaveTextContent("1 项资料已移除或不属于当前账号")
    fireEvent.click(screen.getByText("本次资料 · 1 项"))
    expect(screen.getByRole("link", { name: "Real source" })).toHaveAttribute("href", "https://arxiv.org/abs/2605.22343")
    expect(screen.getByText("Methods to examine")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "用这些资料准备报告" }))
    expect(readResearchWorkspace().reportDrafts?.[0]).toMatchObject({ groupId: "group-a", question: "Agent", materials: [{ id: "paper-a", title: "Real source", url: "https://arxiv.org/abs/2605.22343", notes: "Methods to examine" }] })
    expect(navigation.push).toHaveBeenCalledWith(expect.stringContaining("/reports?"))
  })
  it("removes one reference while preserving discovery state and the original stored source", () => {
    render(<ResearchMaterialContext />)
    fireEvent.click(screen.getByText("本次资料 · 1 项"))
    fireEvent.click(screen.getByRole("button", { name: "取消携带资料：Real source" }))
    const url = new URL(navigation.replace.mock.calls[0][0], "https://agora.invalid")
    expect(url.searchParams.getAll("material")).toEqual(["unknown"])
    expect(url.searchParams.get("researchSession")).toBe("study")
    expect(readResearchWorkspace().materials).toHaveLength(1)
  })
  it("hides selected sources immediately when the owner changes", () => {
    render(<ResearchMaterialContext />)
    act(() => { selectHistoryOwner("other"); acceptAccountHistory({ visits: [], groups: [] }, 0) })
    expect(screen.queryByText("Real source")).not.toBeInTheDocument()
    expect(screen.getByRole("status")).toHaveTextContent("2 项资料")
  })
})
