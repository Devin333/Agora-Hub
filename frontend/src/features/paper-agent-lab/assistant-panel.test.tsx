import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { PREVIEW_PAPERS } from "./data"
import { AssistantPanel } from "./assistant-panel"
import type { PreviewQuestion } from "./types"

const [memgpt, generativeAgents] = PREVIEW_PAPERS
const conversation: readonly PreviewQuestion[] = [
  { id: "q-memgpt", prompt: "MemGPT 如何调度分层记忆？", paperId: memgpt.id },
  { id: "q-agents", prompt: "反思模块如何影响后续计划？", paperId: generativeAgents.id },
]

afterEach(cleanup)

describe("AssistantPanel", () => {
  it("shows only the current paper conversation and an explicit no-service boundary", () => {
    const { rerender } = render(
      <AssistantPanel
        conversation={conversation}
        onClose={() => undefined}
        onSubmit={() => undefined}
        paper={memgpt}
      />,
    )

    expect(screen.getByText(memgpt.titleZh)).toBeVisible()
    expect(screen.getByText("MemGPT 如何调度分层记忆？")).toBeVisible()
    expect(screen.queryByText("反思模块如何影响后续计划？")).not.toBeInTheDocument()
    expect(screen.getAllByText(/不会调用|未调用|未发送/).length).toBeGreaterThan(0)

    rerender(
      <AssistantPanel
        conversation={conversation}
        onClose={() => undefined}
        onSubmit={() => undefined}
        paper={generativeAgents}
      />,
    )

    expect(screen.getByText(generativeAgents.titleZh)).toBeVisible()
    expect(screen.getByText("反思模块如何影响后续计划？")).toBeVisible()
    expect(screen.queryByText("MemGPT 如何调度分层记忆？")).not.toBeInTheDocument()
  })

  it("prefills a suggestion, focuses the composer and consumes that handoff once", async () => {
    const onPromptConsumed = vi.fn()
    render(
      <AssistantPanel
        conversation={[]}
        initialPrompt="请解释外部记忆与工作记忆的边界。"
        onClose={() => undefined}
        onPromptConsumed={onPromptConsumed}
        onSubmit={() => undefined}
        paper={memgpt}
      />,
    )

    expect(screen.getByRole("textbox", { name: "向论文助手提问" })).toHaveValue(
      "请解释外部记忆与工作记忆的边界。",
    )
    await waitFor(() => expect(screen.getByRole("textbox", { name: "向论文助手提问" })).toHaveFocus())
    expect(onPromptConsumed).toHaveBeenCalledTimes(1)
  })

  it("does not submit blank input or an Enter key event during IME composition", () => {
    const onSubmit = vi.fn()
    render(
      <AssistantPanel
        conversation={[]}
        onClose={() => undefined}
        onSubmit={onSubmit}
        paper={memgpt}
      />,
    )

    const input = screen.getByRole("textbox", { name: "向论文助手提问" })
    const submit = screen.getByRole("button", { name: "提交预览问题" })

    expect(submit).toBeDisabled()
    fireEvent.change(input, { target: { value: "   " } })
    fireEvent.click(submit)
    expect(onSubmit).not.toHaveBeenCalled()

    fireEvent.change(input, { target: { value: "中文输入中" } })
    fireEvent.compositionStart(input)
    fireEvent.keyDown(input, { key: "Enter", code: "Enter", keyCode: 229 })
    expect(onSubmit).not.toHaveBeenCalled()
    expect(input).toHaveValue("中文输入中")
  })

  it("submits a trimmed real user question with Enter and clears the composer", () => {
    const onSubmit = vi.fn()
    render(
      <AssistantPanel
        conversation={[]}
        onClose={() => undefined}
        onSubmit={onSubmit}
        paper={memgpt}
      />,
    )

    const input = screen.getByRole("textbox", { name: "向论文助手提问" })
    fireEvent.change(input, { target: { value: "  论文如何验证记忆管理？  " } })
    fireEvent.keyDown(input, { key: "Enter", code: "Enter" })

    expect(onSubmit).toHaveBeenCalledTimes(1)
    expect(onSubmit).toHaveBeenCalledWith("论文如何验证记忆管理？")
    expect(input).toHaveValue("")
  })

  it("keeps Shift+Enter for multiline drafting and exposes a close action", () => {
    const onClose = vi.fn()
    const onSubmit = vi.fn()
    render(
      <AssistantPanel
        conversation={[]}
        onClose={onClose}
        onSubmit={onSubmit}
        paper={memgpt}
      />,
    )

    const input = screen.getByRole("textbox", { name: "向论文助手提问" })
    fireEvent.change(input, { target: { value: "第一行" } })
    fireEvent.keyDown(input, { key: "Enter", code: "Enter", shiftKey: true })
    expect(onSubmit).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole("button", { name: "关闭论文助手" }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})