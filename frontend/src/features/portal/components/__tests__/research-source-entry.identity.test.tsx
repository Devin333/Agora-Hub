import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import * as history from "@/lib/research/history"
import { emptyWorkspace } from "@/lib/research/history-model"
import type { ResearchMaterial } from "@/lib/research/workspace-items"
import { useReaderNotebook } from "@/features/portal/reader/use-reader-notebook"
import { ResearchSourceEntry } from "../research-source-entry"

const profile = vi.hoisted(() => ({ desktop: true }))
vi.mock("@/lib/deployment-profile", async importOriginal => ({
  ...await importOriginal<typeof import("@/lib/deployment-profile")>(),
  isLocalDesktopProfile: () => profile.desktop,
}))
const revision = `sha256:${"a".repeat(64)}`
const original: ResearchMaterial = {
  id: "library-original", groupId: "reading-group", kind: "pdf",
  title: "My custom title", url: "", referenceId: "paper-canonical",
  readerHref: `/papers/paper-canonical/read?sourceRevision=${encodeURIComponent(revision)}`,
  notes: "Keep my notes", favorite: true, readingStatus: "reading",
  tags: ["methods", "review"], createdAt: 1000, updatedAt: 2000,
}
const notebook = {
  paperId: original.referenceId!, sourceRevision: revision, note: "Reader notes",
  question: "My question", fontSize: 18 as const, sectionId: "section-3", pdfPage: 12,
  readerView: "pdf" as const, updatedAt: 2000,
}
function loadLibrary(materials: ResearchMaterial[] = [original]) {
  history.acceptAccountHistory({
    ...emptyWorkspace(),
    groups: [{ id: "reading-group", name: "Reading", createdAt: 1000, updatedAt: 1000 }],
    materials: materials.map(material => ({ ...material })),
    readerNotebooks: [{ ...notebook }],
  }, 7)
}
async function renderReceipt({
  importId = "imp_new", paperId = "paper-canonical", filename = "renamed.pdf",
  sourceRevision = revision, owner = "alice",
} = {}, onAttach = vi.fn()) {
  sessionStorage.setItem(`agora-pdf-import:${owner}`, JSON.stringify({
    importId, paperId, filename, sourceRevision, status: "completed",
  }))
  const view = render(<ResearchSourceEntry onAttach={onAttach} />)
  await screen.findByRole("button", { name: "用于本次研究" })
  return { ...view, onAttach }
}
beforeEach(() => {
  profile.desktop = true
  sessionStorage.clear()
  history.selectHistoryOwner("alice")
  loadLibrary()
})
afterEach(() => {
  cleanup()
  history.selectHistoryOwner(null)
  vi.restoreAllMocks()
})
describe("canonical PDF library attachment", () => {
  it("reuses the existing row for new import receipts and renamed files without losing user metadata", async () => {
    const attach = vi.fn()
    const first = await renderReceipt({ importId: "imp_repeat_one", filename: "renamed-one.pdf" }, attach)
    fireEvent.click(screen.getByRole("button", { name: "用于本次研究" }))
    expect(attach).toHaveBeenLastCalledWith(original.id)
    expect(history.readResearchWorkspace().materials).toHaveLength(1)
    expect(history.readResearchWorkspace().materials![0]).toMatchObject({ ...original, updatedAt: expect.any(Number) })
    expect(history.readResearchWorkspace().readerNotebooks).toEqual([notebook])
    expect(history.historyState()).toMatchObject({ dirty: true, status: "saving", revision: 7 })
    expect(screen.getByRole("button", { name: "入库待保存" })).toBeDisabled()
    act(() => history.acknowledgeAccountHistory(8, history.historyState().generation))
    await waitFor(() => expect(screen.getByRole("button", { name: "已加入本次研究" })).toBeDisabled())
    expect(JSON.parse(sessionStorage.getItem("agora-pdf-import:alice")!)).toMatchObject({ importId: "imp_repeat_one" })
    first.unmount()
    await renderReceipt({ importId: "imp_repeat_two", filename: "renamed-two.pdf" }, attach)
    fireEvent.click(screen.getByRole("button", { name: "用于本次研究" }))
    expect(attach).toHaveBeenLastCalledWith(original.id)
    expect(history.readResearchWorkspace().materials).toHaveLength(1)
    expect(history.readResearchWorkspace().materials![0]).toMatchObject({ ...original, updatedAt: expect.any(Number) })
    expect(JSON.parse(sessionStorage.getItem("agora-pdf-import:alice")!)).toMatchObject({ importId: "imp_repeat_two" })
  })
  it("adds a different canonical paper even when its filename matches the existing title", async () => {
    await renderReceipt({ paperId: "paper-different", filename: original.title })
    fireEvent.click(screen.getByRole("button", { name: "用于本次研究" }))
    const materials = history.readResearchWorkspace().materials!
    expect(materials).toHaveLength(2)
    expect(materials.find(material => material.id === original.id)).toMatchObject(original)
    expect(materials.find(material => material.id === "imp_new")).toMatchObject({
      referenceId: "paper-different", title: original.title, groupId: null, notes: "",
    })
  })
  it("deduplicates the same canonical paper across paper and PDF material kinds", async () => {
    loadLibrary([{ ...original, kind: "paper" }])
    const view = await renderReceipt()
    fireEvent.click(screen.getByRole("button", { name: "用于本次研究" }))
    expect(view.onAttach).toHaveBeenCalledWith(original.id)
    expect(history.readResearchWorkspace().materials).toHaveLength(1)
    expect(history.readResearchWorkspace().materials![0]).toMatchObject({ ...original, kind: "paper", updatedAt: expect.any(Number) })
  })
  it("updates a new revision locator and lets the notebook contract reset old page positions", async () => {
    const nextRevision = `sha256:${"b".repeat(64)}`
    const view = await renderReceipt({ sourceRevision: nextRevision })
    fireEvent.click(screen.getByRole("button", { name: "用于本次研究" }))
    expect(view.onAttach).toHaveBeenCalledWith(original.id)
    const material = history.readResearchWorkspace().materials![0]
    expect(material).toMatchObject({ ...original, updatedAt: expect.any(Number), readerHref: expect.stringContaining(encodeURIComponent(nextRevision)) })
    expect(history.readResearchWorkspace().readerNotebooks).toEqual([notebook])
    const restored = renderHook(() => useReaderNotebook(material.referenceId!, nextRevision))
    await waitFor(() => expect(restored.result.current.ready).toBe(true))
    expect(restored.result.current).toMatchObject({
      sourceRevision: nextRevision, note: notebook.note, question: notebook.question,
      sectionId: "", pdfPage: 1, readerView: "text",
    })
  })
  it.each(["not-ready", "other-owner"])("rejects a stale attach when the live workspace is %s", async condition => {
    const view = await renderReceipt()
    const state = history.historyState()
    vi.spyOn(history, "historyState").mockReturnValue({
      ...state, ready: condition !== "not-ready", owner: condition === "other-owner" ? "bob" : state.owner,
    })
    const save = vi.spyOn(history, "saveResearchMaterial")
    fireEvent.click(screen.getByRole("button", { name: "用于本次研究" }))
    expect(save).not.toHaveBeenCalled()
    expect(view.onAttach).not.toHaveBeenCalled()
    expect(history.readResearchWorkspace().materials).toEqual([original])
  })
  it("does not reuse the previous owner's row after an account switch", async () => {
    const aliceView = await renderReceipt()
    act(() => { history.selectHistoryOwner("bob"); loadLibrary([]) })
    expect(screen.queryByRole("button", { name: "用于本次研究" })).not.toBeInTheDocument()
    expect(aliceView.onAttach).not.toHaveBeenCalled()
    aliceView.unmount()
    const bobView = await renderReceipt({ owner: "bob", importId: "imp_bob" })
    fireEvent.click(screen.getByRole("button", { name: "用于本次研究" }))
    expect(bobView.onAttach).toHaveBeenCalledWith("imp_bob")
    expect(history.readResearchWorkspace().materials).toHaveLength(1)
    expect(history.readResearchWorkspace().materials![0].id).toBe("imp_bob")
  })
  it("keeps a failed save unattached and retains the receipt for retry", async () => {
    const view = await renderReceipt()
    const save = vi.spyOn(history, "saveResearchMaterial").mockReturnValueOnce(false)
    fireEvent.click(screen.getByRole("button", { name: "用于本次研究" }))
    expect(view.onAttach).not.toHaveBeenCalled()
    expect(history.readResearchWorkspace().materials).toEqual([original])
    expect(screen.getByRole("alert")).toHaveTextContent("暂时无法引用 PDF")
    expect(screen.getByRole("button", { name: "用于本次研究" })).toBeEnabled()
    expect(JSON.parse(sessionStorage.getItem("agora-pdf-import:alice")!)).toMatchObject({ importId: "imp_new" })
    save.mockRestore()
    fireEvent.click(screen.getByRole("button", { name: "用于本次研究" }))
    expect(view.onAttach).toHaveBeenCalledWith(original.id)
  })
  it("preserves the default portal contract without adding a desktop reader locator", async () => {
    profile.desktop = false
    loadLibrary([])
    const view = await renderReceipt()
    fireEvent.click(screen.getByRole("button", { name: "用于本次研究" }))
    expect(view.container.querySelector("[data-research-source-entry]")).toHaveAttribute("data-appearance", "portal")
    expect(screen.queryByRole("link", { name: "打开阅读器" })).not.toBeInTheDocument()
    expect(history.readResearchWorkspace().materials![0].readerHref).toBeUndefined()
    expect(view.onAttach).toHaveBeenCalledWith("imp_new")
  })
})
