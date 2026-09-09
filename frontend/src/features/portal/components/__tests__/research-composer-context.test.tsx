import { useState } from "react"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ResearchComposerContext, ResearchContextSummary, supportedResearchLink } from "../research-composer-context"
import { readResearchWorkspace, saveResearchGroup, selectHistoryOwner } from "@/lib/research/history"

function Composer() {
  const [question, setQuestion] = useState("找 Agent 相关的论文")
  const [ids, setIds] = useState<string[]>([])
  const [groupId, setGroupId] = useState<string | null>("agents")
  const context = { groupId, setGroupId, materialIds: ids, setMaterialIds: setIds, disabled: false }
  return <><form onSubmit={event => { event.preventDefault(); throw new Error("Adding context must not submit the question") }}><ResearchComposerContext {...context} /><input aria-label="问题" value={question} onChange={e => setQuestion(e.target.value)} /></form><ResearchContextSummary {...context} /></>
}

describe("composer context", () => {
  beforeEach(() => { localStorage.clear(); selectHistoryOwner(null); saveResearchGroup("Agent", "agents"); Element.prototype.hasPointerCapture = vi.fn(); Element.prototype.setPointerCapture = vi.fn(); Element.prototype.releasePointerCapture = vi.fn(); Element.prototype.scrollIntoView = vi.fn() })
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it("validates supported source identities instead of accepting arbitrary fetch targets", () => {
    expect(supportedResearchLink("https://arxiv.org/pdf/2605.22343.pdf")).toBe("https://arxiv.org/pdf/2605.22343.pdf")
    expect(supportedResearchLink("https://doi.org/10.1000/example")).toBe("https://doi.org/10.1000/example")
    expect(supportedResearchLink("https://github.com/openai/codex?tab=readme")).toBe("https://github.com/openai/codex")
    for (const value of ["javascript:alert(1)", "https://github.com.evil.test/a/b", "http://127.0.0.1/paper", "https://user:pass@github.com/a/b", "https://github.com/a/b/issues/1"]) expect(supportedResearchLink(value)).toBeNull()
  })

  it("attaches a link separately, keeps the question, and does not turn the storage group into context", async () => {
    render(<Composer />)
    expect(screen.getByLabelText("本次研究选中的资料")).toHaveTextContent("保存到：Agent")
    fireEvent.pointerDown(screen.getByRole("button", { name: "添加到本次研究" }), { button: 0, ctrlKey: false, pointerType: "mouse" })
    fireEvent.click(await screen.findByRole("menuitem", { name: "添加论文或项目链接" }))
    fireEvent.change(await screen.findByRole("textbox", { name: "链接" }), { target: { value: "https://github.com/openai/codex" } })
    fireEvent.click(screen.getByRole("button", { name: "添加", exact: true }))
    await waitFor(() => expect(screen.getByLabelText("本次研究选中的资料")).toHaveTextContent("openai/codex"))
    expect(screen.getByRole("textbox", { name: "问题" })).toHaveValue("找 Agent 相关的论文")
    expect(readResearchWorkspace().materials?.[0].groupId).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: "取消携带资料：openai/codex" }))
    expect(screen.queryByRole("button", { name: "取消携带资料：openai/codex" })).not.toBeInTheDocument()
    expect(readResearchWorkspace().materials).toHaveLength(1)
    act(() => selectHistoryOwner("other-account"))
    expect(screen.queryByLabelText("本次研究选中的资料")).not.toBeInTheDocument()
  })
})
