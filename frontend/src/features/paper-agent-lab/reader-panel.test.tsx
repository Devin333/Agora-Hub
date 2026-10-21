import { useState } from "react"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { PREVIEW_PAPERS } from "./data"
import { ReaderPanel } from "./reader-panel"
import type { ReaderTab } from "./types"

const paper = PREVIEW_PAPERS[0]

interface HarnessProps {
  initialTab?: ReaderTab
  initialSaved?: boolean
  initialRead?: boolean
  initialFocused?: boolean
  onAsk?: (prompt?: string) => void
}

function ReaderHarness({
  initialTab = "overview",
  initialSaved = false,
  initialRead = false,
  initialFocused = false,
  onAsk = () => undefined,
}: HarnessProps) {
  const [tab, setTab] = useState<ReaderTab>(initialTab)
  const [saved, setSaved] = useState(initialSaved)
  const [read, setRead] = useState(initialRead)
  const [focused, setFocused] = useState(initialFocused)
  const [note, setNote] = useState("")

  return (
    <ReaderPanel
      focused={focused}
      note={note}
      onAsk={onAsk}
      onFocusToggle={() => setFocused((value) => !value)}
      onNoteChange={setNote}
      onTabChange={setTab}
      onToggleRead={() => setRead((value) => !value)}
      onToggleSave={() => setSaved((value) => !value)}
      paper={paper}
      read={read}
      saved={saved}
      tab={tab}
    />
  )
}

afterEach(cleanup)

describe("ReaderPanel", () => {
  it("renders the selected real paper and exposes an honest preview boundary", () => {
    render(<ReaderHarness />)

    expect(screen.getByRole("heading", { name: paper.title })).toBeVisible()
    expect(screen.getByText(paper.authors)).toBeVisible()
    expect(screen.getByText(paper.summary)).toBeVisible()
    expect(screen.getByText("文献导读 / 预置示例")).toBeVisible()
    expect(screen.getByText("本页使用公开文献信息展示阅读排版，不是论文全文，也未连接研究服务。")).toBeVisible()
    expect(screen.getByRole("link", { name: new RegExp(paper.arxivId) })).toHaveAttribute("href", paper.sourceUrl)
  })

  it("switches tabs with clicks and the complete keyboard tab pattern", () => {
    render(<ReaderHarness />)

    const overview = screen.getByRole("tab", { name: "概览" })
    const notes = screen.getByRole("tab", { name: "笔记" })
    const details = screen.getByRole("tab", { name: "详情" })

    overview.focus()
    fireEvent.keyDown(overview, { key: "ArrowRight" })
    expect(notes).toHaveFocus()
    expect(notes).toHaveAttribute("aria-selected", "true")
    expect(screen.getByRole("textbox", { name: "本篇笔记" })).toBeVisible()

    fireEvent.keyDown(notes, { key: "End" })
    expect(details).toHaveFocus()
    expect(details).toHaveAttribute("aria-selected", "true")
    expect(screen.getByText(paper.arxivId)).toBeVisible()

    fireEvent.keyDown(details, { key: "ArrowRight" })
    expect(overview).toHaveFocus()
    expect(overview).toHaveAttribute("aria-selected", "true")

    fireEvent.keyDown(overview, { key: "ArrowLeft" })
    expect(details).toHaveFocus()
    fireEvent.keyDown(details, { key: "Home" })
    expect(overview).toHaveFocus()
  })

  it("edits the note as controlled page-memory state and reports unicode character length", () => {
    render(<ReaderHarness initialTab="notes" />)

    const note = screen.getByRole("textbox", { name: "本篇笔记" })
    fireEvent.change(note, { target: { value: "记忆分层🧠" } })

    expect(note).toHaveValue("记忆分层🧠")
    expect(screen.getByText("输入字数：5")).toBeVisible()
    expect(screen.getByText("仅本次预览保留，不写入长期记忆。")).toBeVisible()
  })

  it("toggles read, saved and focus state through stable accessible actions", () => {
    render(<ReaderHarness />)

    fireEvent.click(screen.getByRole("button", { name: "标为已读" }))
    expect(screen.getByRole("button", { name: "标为未读" })).toHaveAttribute("aria-pressed", "true")

    fireEvent.click(screen.getByRole("button", { name: `收藏：${paper.title}` }))
    expect(screen.getByRole("button", { name: `取消收藏：${paper.title}` })).toHaveAttribute("aria-pressed", "true")

    fireEvent.click(screen.getByRole("button", { name: "专注阅读" }))
    expect(screen.getByRole("button", { name: "退出专注阅读" })).toHaveAttribute("aria-pressed", "true")
  })

  it("resets reader scroll for a new paper or tab but not unrelated state changes", () => {
    const noop = () => undefined
    const props = {
      focused: false,
      note: "",
      onAsk: noop,
      onFocusToggle: noop,
      onNoteChange: noop,
      onTabChange: noop,
      onToggleRead: noop,
      onToggleSave: noop,
      paper,
      read: false,
      saved: false,
      tab: "overview" as const,
    }
    const { container, rerender } = render(<ReaderPanel {...props} />)
    const scroller = container.querySelector('[role="tabpanel"]')?.parentElement as HTMLElement

    scroller.scrollTop = 240
    rerender(<ReaderPanel {...props} saved />)
    expect(scroller.scrollTop).toBe(240)

    rerender(<ReaderPanel {...props} paper={PREVIEW_PAPERS[1]} />)
    expect(scroller.scrollTop).toBe(0)

    scroller.scrollTop = 180
    rerender(<ReaderPanel {...props} paper={PREVIEW_PAPERS[1]} tab="notes" />)
    expect(scroller.scrollTop).toBe(0)
  })

  it("opens the assistant without a prompt and can prefill a paper-specific suggested question", () => {
    const onAsk = vi.fn()
    render(<ReaderHarness onAsk={onAsk} />)

    fireEvent.click(screen.getByRole("button", { name: "打开论文助手" }))
    expect(onAsk).toHaveBeenLastCalledWith()

    fireEvent.click(screen.getByRole("button", { name: "继续追问" }))
    expect(onAsk).toHaveBeenLastCalledWith(expect.stringContaining(paper.titleZh))
  })
})