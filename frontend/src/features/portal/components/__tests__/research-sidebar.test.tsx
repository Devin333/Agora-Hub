import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ResearchSidebar, researchDateSection } from "../research-sidebar"
import { readResearchHistory, readResearchWorkspace, recordResearchVisit, saveResearchGroup, selectHistoryOwner } from "@/lib/research/history"
import { researchQuestionHref } from "@/lib/research/entry"

function renderSidebar() { return render(<ResearchSidebar collapsed={false} onToggle={vi.fn()} onNew={vi.fn()} onResume={vi.fn()} />) }
async function menu(name: string) {
  await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument())
  fireEvent.keyDown(screen.getByRole("button", { name }), { key: "ArrowDown" })
  await screen.findByRole("menu")
}
describe("research history sidebar", () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear(); selectHistoryOwner(null); recordResearchVisit(researchQuestionHref("papers", "Agent", "study")) })
  afterEach(cleanup)
  it("searches titles and questions and explains an empty result", () => {
    recordResearchVisit(researchQuestionHref("projects", "开源框架", "project"))
    renderSidebar()
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索研究历史" }), { target: { value: "Agent" } })
    expect(screen.getByRole("button", { name: "继续研究：Agent" })).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "继续研究：开源框架" })).not.toBeInTheDocument()
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "不存在" } })
    expect(screen.getByText("没有找到相关研究")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "查看全部记录" }))
    expect(screen.getByRole("button", { name: "继续研究：开源框架" })).toBeInTheDocument()
  })
  it("supports keyboard menus, rename, favorite, group moves, and deletion undo", async () => {
    renderSidebar()
    fireEvent.click(screen.getByRole("button", { name: "新建分组" }))
    fireEvent.change(screen.getByRole("textbox", { name: "分组名称" }), { target: { value: "Agent 调研" } })
    fireEvent.click(screen.getByRole("button", { name: "保存" }))
    expect(readResearchWorkspace().groups[0].name).toBe("Agent 调研")
    await menu("研究操作：Agent")
    fireEvent.click(screen.getByRole("menuitem", { name: "重命名" }))
    fireEvent.change(screen.getByRole("textbox", { name: "研究标题" }), { target: { value: "评测资料" } })
    fireEvent.click(screen.getByRole("button", { name: "保存" }))
    await menu("研究操作：评测资料")
    fireEvent.click(screen.getByRole("menuitem", { name: "收藏", exact: true }))
    expect(readResearchHistory()[0].isFavorite).toBe(true)
    await menu("研究操作：评测资料")
    fireEvent.click(screen.getByRole("menuitem", { name: "移动到分组" }))
    fireEvent.change(screen.getByRole("combobox", { name: "目标分组" }), { target: { value: readResearchWorkspace().groups[0].id } })
    fireEvent.click(screen.getByRole("button", { name: "保存" }))
    expect(readResearchHistory()[0].groupId).toBe(readResearchWorkspace().groups[0].id)
    await menu("研究操作：评测资料")
    fireEvent.click(screen.getByRole("menuitem", { name: "删除", exact: true }))
    expect(screen.queryByRole("button", { name: "继续研究：评测资料" })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "撤销" }))
    expect(screen.getByRole("button", { name: "继续研究：评测资料" })).toBeInTheDocument()
    await menu("分组操作：Agent 调研")
    fireEvent.click(screen.getByRole("menuitem", { name: "删除分组" }))
    expect(screen.getByText(/不会被删除/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "删除分组", exact: true }))
    await waitFor(() => expect(readResearchWorkspace().groups).toHaveLength(0))
    expect(readResearchHistory()[0]).toMatchObject({ groupId: null, isFavorite: true, title: "评测资料" })
  })
  it("initially shows five groups and expands the rest", () => {
    for (let i = 0; i < 6; i++) saveResearchGroup(`课题 ${i}`, `group-${i}`)
    renderSidebar()
    expect(screen.queryByTitle("课题 5")).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /其余 1 个分组/ }))
    expect(screen.getByTitle("课题 5")).toBeInTheDocument()
    act(() => selectHistoryOwner("another-user"))
    expect(screen.queryByTitle("课题 5")).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "继续研究：Agent" })).not.toBeInTheDocument()
  })
  it("groups by calendar date rather than elapsed hours", () => {
    const now = new Date(2026, 8, 8, 0, 5)
    expect(researchDateSection(new Date(2026, 8, 7, 23, 55).getTime(), now)).toBe("昨天")
    expect(researchDateSection(new Date(2026, 8, 1, 23, 55).getTime(), now)).toBe("更早")
  })
})
