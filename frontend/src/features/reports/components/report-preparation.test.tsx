import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { StrictMode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ReportPreparation } from "./report-preparation"
import { readReportDrafts } from "../lib/report-drafts"

describe("report preparation", () => {
  beforeEach(() => localStorage.clear())
  afterEach(() => { cleanup(); vi.restoreAllMocks() })
  it("prefills the topic and restores saved source material, scope and notes", async () => {
    const saved = vi.fn()
    const first = render(<StrictMode><ReportPreparation question="比较 RAG 方法" onSaved={saved} /></StrictMode>)
    expect(await screen.findByRole("textbox", { name: "研究主题" })).toHaveValue("比较 RAG 方法")
    fireEvent.change(screen.getByRole("textbox", { name: "研究范围" }), { target: { value: "2025 年以来的评测" } })
    fireEvent.click(screen.getByRole("button", { name: "添加资料" }))
    fireEvent.change(screen.getByRole("textbox", { name: "资料 1 标题" }), { target: { value: "原始论文" } })
    fireEvent.change(screen.getByRole("textbox", { name: "资料 1 链接" }), { target: { value: "https://arxiv.org/abs/2401.00001" } })
    fireEvent.change(screen.getByRole("textbox", { name: "资料 1 摘要" }), { target: { value: "需要复核实验设置" } })
    fireEvent.change(screen.getByRole("textbox", { name: "分析笔记" }), { target: { value: "比较召回与引用质量" } })
    fireEvent.click(screen.getAllByRole("button", { name: "保存草稿" })[0])
    expect(saved).toHaveBeenCalledOnce()
    expect(screen.getByRole("status")).toHaveTextContent("已保存到本机")
    const [draft] = readReportDrafts()
    first.unmount()
    render(<ReportPreparation question="比较 RAG 方法" draftId={draft.id} onSaved={vi.fn()} />)
    await waitFor(() => expect(screen.getByRole("textbox", { name: "分析笔记" })).toHaveValue("比较召回与引用质量"))
    expect(screen.getByRole("textbox", { name: "研究范围" })).toHaveValue("2025 年以来的评测")
    expect(screen.getByRole("textbox", { name: "资料 1 摘要" })).toHaveValue("需要复核实验设置")
    expect(screen.getByRole("link", { name: "查找相关论文" })).toHaveAttribute("target", "_blank")
    fireEvent.click(screen.getByRole("button", { name: "删除草稿：比较 RAG 方法" }))
    expect(readReportDrafts()).toEqual([])
  })
  it("rejects unsafe source URLs without losing edits or claiming success", async () => {
    render(<ReportPreparation question="主题" onSaved={vi.fn()} />)
    fireEvent.click(await screen.findByRole("button", { name: "添加资料" }))
    fireEvent.change(screen.getByRole("textbox", { name: "资料 1 链接" }), { target: { value: "javascript:alert(1)" } })
    fireEvent.click(screen.getAllByRole("button", { name: "保存草稿" })[0])
    expect(screen.getByRole("alert")).toHaveTextContent("http:// 或 https://")
    expect(readReportDrafts()).toEqual([])
    expect(screen.getByRole("textbox", { name: "资料 1 链接" })).toHaveValue("javascript:alert(1)")
  })
  it("reports storage failure and retains the topic", async () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("full") })
    render(<ReportPreparation question="需保存的主题" onSaved={vi.fn()} />)
    fireEvent.click((await screen.findAllByRole("button", { name: "保存草稿" }))[0])
    expect(screen.getByRole("alert")).toHaveTextContent("草稿未能保存")
    expect(screen.getByRole("textbox", { name: "研究主题" })).toHaveValue("需保存的主题")
  })
})
