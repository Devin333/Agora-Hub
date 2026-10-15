import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { PersonalMemoryPanel } from "./personal-memory-panel"

const client = vi.hoisted(() => ({
  confirmPersonalMemory: vi.fn(),
  createPersonalMemory: vi.fn(),
  deletePersonalMemory: vi.fn(),
  exportPersonalMemory: vi.fn(),
  getPersonalMemoryInfo: vi.fn(),
  getPersonalMemoryPolicy: vi.fn(),
  listPersonalMemoryPage: vi.fn(),
  rebuildPersonalMemoryIndex: vi.fn(),
  rejectPersonalMemoryCandidate: vi.fn(),
  setPersonalMemoryStatus: vi.fn(),
  updatePersonalMemory: vi.fn(),
  updatePersonalMemoryPolicy: vi.fn(),
}))

vi.mock("./personal-memory-client", () => client)
vi.mock("./user-export", () => ({ saveUserExport: vi.fn() }))

const policy = {
  allow_remote_processing: false,
  remote_processing_permissions: { model: false, tool: false, embedding: false },
  revision: 1,
  created_at: "2026-10-14T08:00:00Z",
  updated_at: "2026-10-14T08:00:00Z",
}
const info = {
  storage: "local_sqlite",
  status: "available",
  data_root: "F:/private/research",
  database_path: "F:/private/research/personal-memory.sqlite3",
}
const savedRecord = {
  memory_id: `pmem_${"a".repeat(32)}`,
  content: "保留真实用户输入",
  category: "research_interest" as const,
  source_refs: [{ source_type: "user_input" as const, source_ref: "paper-agent:test", label: "论文 App 手动输入" }],
  status: "candidate" as const,
  revision: 1,
  created_at: "2026-10-14T08:00:00Z",
  updated_at: "2026-10-14T08:00:00Z",
}

function renderPanel(overrides: Partial<React.ComponentProps<typeof PersonalMemoryPanel>> = {}) {
  const props: React.ComponentProps<typeof PersonalMemoryPanel> = {
    open: true,
    namespace: "research/local-user",
    sourceContext: null,
    onClose: vi.fn(),
    onAuthorizedChange: vi.fn(),
    onPolicyChange: vi.fn(),
    onDraftChange: vi.fn(),
    onActivityChange: vi.fn(),
    ...overrides,
  }
  return { ...render(<PersonalMemoryPanel {...props} />), props }
}

async function waitForInitialLoad() {
  await screen.findByText("没有符合当前视图的真实记忆记录。")
}

describe("PersonalMemoryPanel dialog boundaries", () => {
  beforeEach(() => {
    for (const mock of Object.values(client)) mock.mockReset()
    client.listPersonalMemoryPage.mockResolvedValue({ records: [], total: 0, nextCursor: null })
    client.getPersonalMemoryPolicy.mockResolvedValue(policy)
    client.getPersonalMemoryInfo.mockResolvedValue(info)
    client.rebuildPersonalMemoryIndex.mockResolvedValue({ indexedRecords: 0 })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it("traps forward and reverse Tab, contains external focus, restores focus, and restores inert background", async () => {
    const opener = document.createElement("button")
    opener.textContent = "打开个人记忆"
    document.body.append(opener)
    opener.focus()

    const { unmount } = renderPanel()
    const dialog = screen.getByRole("dialog", { name: "个人记忆" })
    const close = screen.getByRole("button", { name: "关闭个人记忆" })
    await waitFor(() => expect(close).toHaveFocus())
    await waitForInitialLoad()
    expect(opener).toHaveAttribute("inert", "")
    expect(opener).toHaveAttribute("aria-hidden", "true")

    const enabled = Array.from(dialog.querySelectorAll<HTMLElement>(
      'button, input:not([type="hidden"]), select, textarea, a[href], summary, [tabindex], [contenteditable="true"]',
    )).filter(element => element.tabIndex >= 0 && !element.matches(":disabled"))
    expect(enabled.length).toBeGreaterThan(2)

    enabled[0].focus()
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true })
    expect(document.activeElement).toBe(enabled.at(-1))
    fireEvent.keyDown(document, { key: "Tab" })
    expect(document.activeElement).toBe(enabled[0])

    opener.focus()
    expect(dialog).toContainElement(document.activeElement as HTMLElement)

    unmount()
    expect(opener).not.toHaveAttribute("inert")
    expect(opener).not.toHaveAttribute("aria-hidden")
    expect(opener).toHaveFocus()
    opener.remove()
  })

  it("restores pre-existing inert and aria-hidden values", async () => {
    const background = document.createElement("div")
    background.setAttribute("inert", "legacy")
    background.setAttribute("aria-hidden", "false")
    document.body.append(background)

    const { unmount } = renderPanel()
    await screen.findByRole("dialog", { name: "个人记忆" })
    expect(background).toHaveAttribute("inert", "")
    expect(background).toHaveAttribute("aria-hidden", "true")

    unmount()
    expect(background).toHaveAttribute("inert", "legacy")
    expect(background).toHaveAttribute("aria-hidden", "false")
    background.remove()
  })

  it("owns Escape and requests close exactly once", async () => {
    const onClose = vi.fn()
    const appEscape = vi.fn()
    document.addEventListener("keydown", appEscape, true)
    renderPanel({ onClose })
    await waitForInitialLoad()

    fireEvent.keyDown(document, { key: "Escape" })

    expect(onClose).toHaveBeenCalledTimes(1)
    expect(appEscape).not.toHaveBeenCalled()
    document.removeEventListener("keydown", appEscape, true)
  })

  it("routes close button, Escape, and backdrop through one dirty-draft confirmation and returns focus to the draft when cancelled", async () => {
    const onClose = vi.fn()
    const onDraftChange = vi.fn()
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false)
    const { container } = renderPanel({ onClose, onDraftChange })
    await waitForInitialLoad()
    const draft = screen.getByRole("textbox", { name: "新个人记忆" })
    fireEvent.change(draft, { target: { value: "尚未保存的记忆" } })
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(true))

    fireEvent.click(screen.getByRole("button", { name: "关闭个人记忆" }))
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(draft).toHaveFocus()

    fireEvent.keyDown(document, { key: "Escape" })
    expect(confirm).toHaveBeenCalledTimes(2)
    expect(draft).toHaveFocus()

    fireEvent.mouseDown(container.querySelector('[role="presentation"]') as HTMLElement)
    expect(confirm).toHaveBeenCalledTimes(3)
    expect(draft).toHaveFocus()
    expect(draft).toHaveValue("尚未保存的记忆")
    expect(onClose).not.toHaveBeenCalled()

    confirm.mockReturnValue(true)
    fireEvent.click(screen.getByRole("button", { name: "关闭个人记忆" }))
    expect(confirm).toHaveBeenCalledTimes(4)
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(draft).toHaveValue("")
    expect(onDraftChange).toHaveBeenLastCalledWith(false)
  })

  it("keeps failed create input and request identity available for a safe retry", async () => {
    client.createPersonalMemory.mockRejectedValue(new Error("磁盘写入失败"))
    renderPanel()
    await waitForInitialLoad()

    const draft = screen.getByRole("textbox", { name: "新个人记忆" })
    fireEvent.change(draft, { target: { value: savedRecord.content } })
    fireEvent.click(screen.getByRole("button", { name: "保存候选" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("磁盘写入失败")
    expect(draft).toHaveValue(savedRecord.content)
    const firstRequest = client.createPersonalMemory.mock.calls[0][0].request_id
    fireEvent.click(screen.getByRole("button", { name: "保存候选" }))
    await waitFor(() => expect(client.createPersonalMemory).toHaveBeenCalledTimes(2))
    expect(client.createPersonalMemory.mock.calls[1][0].request_id).toBe(firstRequest)

    await waitFor(() => expect(screen.getByRole("button", { name: "保存候选" })).toBeEnabled())
    fireEvent.change(draft, { target: { value: "修改后的新 payload" } })
    fireEvent.click(screen.getByRole("button", { name: "保存候选" }))
    await waitFor(() => expect(client.createPersonalMemory).toHaveBeenCalledTimes(3))
    expect(client.createPersonalMemory.mock.calls[2][0].request_id).not.toBe(firstRequest)
  })

  it("retains inline edit input and requires an explicit rebase after a revision conflict", async () => {
    const latest = { ...savedRecord, content: "服务端较新内容", revision: 2 }
    const updated = { ...latest, content: "冲突后保留的编辑", revision: 3 }
    let resolveUpdate!: (value: typeof updated) => void
    const pendingUpdate = new Promise<typeof updated>(resolve => { resolveUpdate = resolve })
    client.listPersonalMemoryPage
      .mockResolvedValueOnce({ records: [savedRecord], total: 1, nextCursor: null })
      .mockResolvedValueOnce({ records: [latest], total: 1, nextCursor: null })
    client.updatePersonalMemory
      .mockRejectedValueOnce(new Error("revision_conflict"))
      .mockReturnValueOnce(pendingUpdate)
    const onDraftChange = vi.fn()
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false)
    renderPanel({ onDraftChange })
    await screen.findByText(savedRecord.content)

    fireEvent.click(screen.getByRole("button", { name: "编辑" }))
    const editor = screen.getByRole("textbox", { name: "编辑记忆内容" })
    fireEvent.change(editor, { target: { value: updated.content } })
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(true))
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("revision_conflict")
    expect(editor).toHaveValue(updated.content)
    expect(client.updatePersonalMemory).toHaveBeenCalledTimes(1)
    expect(client.updatePersonalMemory).toHaveBeenNthCalledWith(
      1,
      savedRecord.memory_id,
      savedRecord.revision,
      { content: updated.content },
    )
    await waitFor(() => expect(screen.getByRole("button", { name: "刷新" })).toBeEnabled())
    fireEvent.click(screen.getByRole("button", { name: "刷新" }))
    const rebase = await screen.findByRole("button", { name: "基于最新版本重试" })
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled()
    await waitFor(() => expect(rebase).toBeEnabled())
    expect(editor).toHaveValue(updated.content)
    expect(client.updatePersonalMemory).toHaveBeenCalledTimes(1)
    expect(confirm).not.toHaveBeenCalled()

    fireEvent.click(rebase)
    expect(confirm).toHaveBeenLastCalledWith(
      "当前服务端版本 r2。内容：服务端较新内容。以保留的输入覆盖此版本吗？",
    )
    expect(editor).toHaveValue(updated.content)
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled()
    expect(client.updatePersonalMemory).toHaveBeenCalledTimes(1)

    confirm.mockReturnValue(true)
    fireEvent.click(rebase)
    await waitFor(() => expect(screen.getByRole("button", { name: "保存修改" })).toBeEnabled())
    expect(editor).toHaveValue(updated.content)
    expect(client.updatePersonalMemory).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }))

    expect(client.updatePersonalMemory).toHaveBeenCalledTimes(2)
    expect(client.updatePersonalMemory).toHaveBeenLastCalledWith(
      savedRecord.memory_id,
      latest.revision,
      { content: updated.content },
    )
    expect(editor).toHaveValue(updated.content)
    expect(editor).toBeDisabled()
    expect(onDraftChange).toHaveBeenLastCalledWith(true)

    // The editor already contains this text; only the completed record render proves the save.
    await act(async () => { resolveUpdate(updated) })

    await waitFor(() => expect(screen.queryByRole("textbox", { name: "编辑记忆内容" })).not.toBeInTheDocument())
    expect(screen.getByText(updated.content, { selector: "p" })).toBeInTheDocument()
    expect(screen.getByText("r3")).toBeInTheDocument()
    expect(onDraftChange).toHaveBeenLastCalledWith(false)
  })

  it("rejects a candidate explicitly instead of presenting it as a generic delete", async () => {
    client.listPersonalMemoryPage.mockResolvedValue({ records: [savedRecord], total: 1, nextCursor: null })
    client.rejectPersonalMemoryCandidate.mockResolvedValue({ ...savedRecord, status: "deleted", revision: 2 })
    vi.spyOn(window, "confirm").mockReturnValue(true)
    renderPanel()
    await screen.findByText(savedRecord.content)

    expect(screen.queryByRole("button", { name: "删除" })).not.toBeInTheDocument()
    expect(screen.getByText(/paper-agent:test/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "拒绝" }))

    await waitFor(() => expect(client.rejectPersonalMemoryCandidate).toHaveBeenCalledWith(savedRecord.memory_id, savedRecord.revision))
    await waitFor(() => expect(screen.queryByText(savedRecord.content)).not.toBeInTheDocument())
  })

  it("rebuilds the local recall index only on an explicit user action", async () => {
    client.rebuildPersonalMemoryIndex.mockResolvedValue({ indexedRecords: 3 })
    renderPanel()
    await waitForInitialLoad()
    expect(client.rebuildPersonalMemoryIndex).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole("button", { name: "重建召回索引" }))

    await waitFor(() => expect(client.rebuildPersonalMemoryIndex).toHaveBeenCalledTimes(1))
    expect(await screen.findByRole("status")).toHaveTextContent("召回索引已按当前 active revision 重建：3 条。")
  })

  it("shows the three remote-processing channels as denied by default", async () => {
    renderPanel()
    await waitForInitialLoad()
    expect(screen.getByText(/模型 禁止 · 工具 禁止 · embedding 禁止/)).toBeInTheDocument()
  })

  it("keeps the panel open while a real operation is pending and lets that operation finish", async () => {
    let resolveCreate!: (value: typeof savedRecord) => void
    client.createPersonalMemory.mockImplementation(() => new Promise(resolve => { resolveCreate = resolve }))
    const onClose = vi.fn()
    const confirm = vi.spyOn(window, "confirm")
    const { container } = renderPanel({ onClose })
    await waitForInitialLoad()

    fireEvent.change(screen.getByRole("textbox", { name: "新个人记忆" }), { target: { value: savedRecord.content } })
    fireEvent.click(screen.getByRole("button", { name: "保存候选" }))
    expect(client.createPersonalMemory).toHaveBeenCalledTimes(1)

    fireEvent.keyDown(document, { key: "Escape" })
    fireEvent.click(screen.getByRole("button", { name: "关闭个人记忆" }))
    fireEvent.mouseDown(container.querySelector('[role="presentation"]') as HTMLElement)

    expect(onClose).not.toHaveBeenCalled()
    expect(confirm).not.toHaveBeenCalled()
    expect(screen.getByRole("status")).toHaveTextContent("操作正在进行；个人记忆面板保持打开，完成后可关闭。")

    resolveCreate(savedRecord)
    expect(await screen.findByText(savedRecord.content)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "关闭个人记忆" }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
