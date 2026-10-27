import { act, cleanup, renderHook, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import * as history from "@/lib/research/history"
import { emptyWorkspace } from "@/lib/research/history-model"
import { retryHistoryEvent } from "@/lib/research/history-sync"
import type { ResearchReaderNotebook } from "@/lib/research/workspace-items"
import { useReaderNotebook } from "./use-reader-notebook"

vi.mock("@/components/auth/portal-account-provider", () => ({ usePortalAccount: () => null }))
vi.mock("@/lib/deployment-profile", () => ({ isHostedProfile: () => true }))

let ownerSequence = 0
const sourceRevision = "revision-1"
const saved: ResearchReaderNotebook = { paperId: "paper-1", sourceRevision, note: "server note", question: "server question", fontSize: 18, sectionId: "methods", pdfPage: 7, readerView: "text", updatedAt: 1 }
const workspace = (notebook = saved) => ({ ...emptyWorkspace(), readerNotebooks: [{ ...notebook }] })

beforeEach(() => {
  sessionStorage.clear(); localStorage.clear()
  history.selectHistoryOwner(`notebook-owner-${++ownerSequence}`)
  history.acceptAccountHistory(workspace(), 7)
})
afterEach(() => { cleanup(); history.selectHistoryOwner(null); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe("owned notebook unsaved draft lifecycle", () => {
  it("retains a rejected local write across restore and remount, then clears only on an exact newer receipt", async () => {
    const rejected = vi.spyOn(history, "saveReaderNotebook").mockReturnValueOnce(false)
    const first = renderHook(() => useReaderNotebook("paper-1", sourceRevision))
    await waitFor(() => expect(first.result.current.ready).toBe(true))
    act(() => first.result.current.update({ note: "failed draft" }))
    expect(first.result.current.note).toBe("failed draft")
    expect(first.result.current.dirty).toBe(true)
    expect(first.result.current.storageError).toBe(true)
    first.unmount()

    const second = renderHook(() => useReaderNotebook("paper-1", sourceRevision))
    await waitFor(() => expect(second.result.current.note).toBe("failed draft"))
    expect(second.result.current.saveStatus).toBe("error")
    rejected.mockRestore()
    act(() => { expect(second.result.current.retrySave()).toBe(true) })
    expect(second.result.current.dirty).toBe(true)
    act(() => history.acknowledgeAccountHistory(8, history.historyState().generation))
    await waitFor(() => expect(second.result.current.dirty).toBe(false))
    expect(second.result.current.saveStatus).toBe("synced")
    expect(second.result.current.note).toBe("failed draft")
  })

  it("does not replace a dirty draft with a later server notebook or stale acknowledgement", async () => {
    const view = renderHook(() => useReaderNotebook("paper-1", sourceRevision))
    await waitFor(() => expect(view.result.current.ready).toBe(true))
    act(() => view.result.current.update({ note: "my local draft" }))
    act(() => history.acceptAccountHistory(workspace({ ...saved, note: "other editor", updatedAt: 5 }), 8))
    await waitFor(() => expect(view.result.current.saveStatus).toBe("conflict"))
    expect(view.result.current.note).toBe("my local draft")
    expect(view.result.current.dirty).toBe(true)
    act(() => { expect(view.result.current.retrySave()).toBe(false) })
    expect(view.result.current.note).toBe("my local draft")
  })

  it("keeps the newest edit dirty through a stale receipt and clears only its exact newer receipt", async () => {
    const view = renderHook(() => useReaderNotebook("paper-1", sourceRevision))
    await waitFor(() => expect(view.result.current.ready).toBe(true))
    act(() => view.result.current.update({ note: "first pending edit" }))
    const earlierGeneration = history.historyState().generation
    act(() => view.result.current.update({ note: "newest pending edit" }))
    const currentGeneration = history.historyState().generation
    expect(currentGeneration).not.toBe(earlierGeneration)

    act(() => history.acknowledgeAccountHistory(8, earlierGeneration))
    expect(view.result.current.note).toBe("newest pending edit")
    expect(view.result.current.dirty).toBe(true)
    expect(view.result.current.saveStatus).toBe("saving")
    expect(history.historyState().dirty).toBe(true)

    act(() => history.acknowledgeAccountHistory(9, currentGeneration))
    await waitFor(() => expect(view.result.current.dirty).toBe(false))
    expect(view.result.current.note).toBe("newest pending edit")
    expect(view.result.current.saveStatus).toBe("synced")
  })

  it("explicitly rebases the preserved draft against a fetched latest revision before retry", async () => {
    const view = renderHook(() => useReaderNotebook("paper-1", sourceRevision))
    await waitFor(() => expect(view.result.current.ready).toBe(true))
    act(() => view.result.current.update({ note: "keep my draft" }))
    act(() => history.setHistoryStatus("conflict"))
    const latest = workspace({ ...saved, note: "remote updated", updatedAt: 10 })
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ success: true, data: { ...latest, revision: 12 } }))))
    const retried = vi.fn()
    window.addEventListener(retryHistoryEvent, retried)
    try {
      await act(async () => { expect(await view.result.current.resolveConflict("keep-draft")).toBe(true) })
      expect(fetch).toHaveBeenCalledTimes(1)
      expect(history.historyState().revision).toBe(12)
      expect(view.result.current.note).toBe("keep my draft")
      expect(view.result.current.dirty).toBe(true)
      expect(retried).toHaveBeenCalledTimes(1)
      act(() => history.acknowledgeAccountHistory(13, history.historyState().generation))
      await waitFor(() => expect(view.result.current.dirty).toBe(false))
    } finally { window.removeEventListener(retryHistoryEvent, retried) }
  })

  it("adopts the server version only after an explicit discard choice", async () => {
    const view = renderHook(() => useReaderNotebook("paper-1", sourceRevision))
    await waitFor(() => expect(view.result.current.ready).toBe(true))
    act(() => view.result.current.update({ note: "discard me explicitly" }))
    act(() => history.setHistoryStatus("conflict"))
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ success: true, data: { ...workspace({ ...saved, note: "remote choice", updatedAt: 10 }), revision: 9 } }))))
    await act(async () => { expect(await view.result.current.resolveConflict("use-server")).toBe(true) })
    expect(view.result.current.note).toBe("remote choice")
    expect(view.result.current.dirty).toBe(false)
    expect(history.historyState().dirty).toBe(false)
  })

  it("refuses to discard unrelated workspace edits while resolving one notebook", async () => {
    const view = renderHook(() => useReaderNotebook("paper-1", sourceRevision))
    await waitFor(() => expect(view.result.current.ready).toBe(true))
    act(() => view.result.current.update({ note: "keep all drafts" }))
    act(() => {
      history.saveResearchMaterial({ id: "other-material", groupId: null, kind: "paper", title: "Other", url: "https://example.org/other", notes: "", createdAt: 1, updatedAt: 1 })
      history.setHistoryStatus("conflict")
    })
    vi.stubGlobal("fetch", vi.fn())
    await act(async () => { expect(await view.result.current.resolveConflict("keep-draft")).toBe(false) })
    expect(fetch).not.toHaveBeenCalled()
    expect(view.result.current.note).toBe("keep all drafts")
    expect(view.result.current.saveError).toContain("其他待保存")
  })

  it("keeps over-limit input without overwriting the server and isolates owner/paper/source scopes", async () => {
    const view = renderHook(({ paperId, revision }) => useReaderNotebook(paperId, revision), { initialProps: { paperId: "paper-1", revision: sourceRevision } })
    await waitFor(() => expect(view.result.current.ready).toBe(true))
    const longDraft = "字".repeat(20001)
    act(() => view.result.current.update({ note: longDraft }))
    expect(view.result.current.noteOverLimit).toBe(true)
    expect(history.historyState().workspace.readerNotebooks?.[0]?.note).toBe("server note")
    view.rerender({ paperId: "paper-2", revision: sourceRevision })
    await waitFor(() => expect(view.result.current.ready).toBe(true))
    expect(view.result.current.note).toBe("")
    view.rerender({ paperId: "paper-1", revision: "revision-2" })
    await waitFor(() => expect(view.result.current.ready).toBe(true))
    expect(view.result.current.note).toBe("server note")
    expect(view.result.current.pdfPage).toBe(1)
    expect(view.result.current.sectionId).toBe("")
    act(() => { history.selectHistoryOwner("isolated-new-owner"); history.acceptAccountHistory(emptyWorkspace(), 0) })
    await waitFor(() => expect(view.result.current.ready).toBe(true))
    expect(view.result.current.note).toBe("")
  })

  it("ignores a late conflict fetch after owner changes", async () => {
    let finish: (value: Response) => void = () => {}
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(resolve => { finish = resolve })))
    const view = renderHook(() => useReaderNotebook("paper-1", sourceRevision))
    await waitFor(() => expect(view.result.current.ready).toBe(true))
    act(() => view.result.current.update({ note: "private old owner draft" }))
    act(() => history.setHistoryStatus("conflict"))
    let resolving: Promise<boolean>
    act(() => { resolving = view.result.current.resolveConflict("use-server") })
    act(() => { history.selectHistoryOwner("owner-after-late-fetch"); history.acceptAccountHistory(emptyWorkspace(), 0) })
    await act(async () => { finish(new Response(JSON.stringify({ success: true, data: { ...workspace(), revision: 99 } }))); expect(await resolving!).toBe(false) })
    expect(history.historyState().owner).toBe("owner-after-late-fetch")
    expect(history.historyState().revision).toBe(0)
    expect(view.result.current.note).toBe("")
  })
})

it("retains a draft after conflict fetch failure and permits an explicit resolution retry", async () => {
  const view = renderHook(() => useReaderNotebook("paper-1", sourceRevision))
  await waitFor(() => expect(view.result.current.ready).toBe(true))
  act(() => view.result.current.update({ note: "draft after transport failure" }))
  act(() => history.setHistoryStatus("conflict"))
  vi.stubGlobal("fetch", vi.fn().mockRejectedValueOnce(new Error("offline while resolving")).mockResolvedValueOnce(new Response(JSON.stringify({ success: true, data: { ...workspace(), revision: 10 } }))))
  await act(async () => { expect(await view.result.current.resolveConflict("keep-draft")).toBe(false) })
  expect(view.result.current.note).toBe("draft after transport failure")
  expect(view.result.current.saveError).toContain("offline while resolving")
  expect(view.result.current.resolving).toBe(false)
  await act(async () => { expect(await view.result.current.resolveConflict("keep-draft")).toBe(true) })
  expect(view.result.current.note).toBe("draft after transport failure")
  expect(history.historyState().revision).toBe(10)
  expect(view.result.current.dirty).toBe(true)
})

it("warns before closing an unqueued failed draft and keeps it through an asynchronous restore", async () => {
  vi.spyOn(history, "saveReaderNotebook").mockReturnValue(false)
  const view = renderHook(() => useReaderNotebook("paper-1", sourceRevision))
  await waitFor(() => expect(view.result.current.ready).toBe(true))
  act(() => view.result.current.update({ note: "not enqueued anywhere" }))
  const unload = new Event("beforeunload", { cancelable: true })
  window.dispatchEvent(unload)
  expect(unload.defaultPrevented).toBe(true)
  act(() => history.acceptAccountHistory(workspace({ ...saved, note: "async restored" }), 8))
  await waitFor(() => expect(view.result.current.saveStatus).toBe("conflict"))
  expect(view.result.current.note).toBe("not enqueued anywhere")
})

it("does not revive a previous workspace-generation draft after the same owner is restored", async () => {
  vi.spyOn(history, "saveReaderNotebook").mockReturnValue(false)
  const first = renderHook(() => useReaderNotebook("paper-1", sourceRevision))
  await waitFor(() => expect(first.result.current.ready).toBe(true))
  act(() => first.result.current.update({ note: "draft from prior workspace generation" }))
  expect(first.result.current.dirty).toBe(true)
  first.unmount()

  // The account store was restored again for the same owner. A module-held
  // draft from the previous store generation is not an official persistence source.
  act(() => history.acceptAccountHistory(workspace({ ...saved, note: "restored owner notebook", updatedAt: 20 }), 11))
  const restored = renderHook(() => useReaderNotebook("paper-1", sourceRevision))
  await waitFor(() => expect(restored.result.current.ready).toBe(true))
  expect(restored.result.current.note).toBe("restored owner notebook")
  expect(restored.result.current.dirty).toBe(false)
  expect(restored.result.current.saveStatus).toBe("synced")
})

it("keeps a mounted dirty draft visible but requires explicit resolution after a same-revision restore", async () => {
  const view = renderHook(() => useReaderNotebook("paper-1", sourceRevision))
  await waitFor(() => expect(view.result.current.ready).toBe(true))
  act(() => view.result.current.update({ note: "mounted dirty draft" }))
  act(() => history.acceptAccountHistory(workspace(), 7))
  await waitFor(() => expect(view.result.current.saveStatus).toBe("conflict"))
  expect(view.result.current.note).toBe("mounted dirty draft")
  const before = history.historyState().generation
  act(() => { expect(view.result.current.retrySave()).toBe(false) })
  expect(history.historyState().generation).toBe(before)
  expect(history.historyState().workspace.readerNotebooks?.[0]?.note).toBe("server note")
})

it("retains an enqueued dirty draft on an ordinary remount within its saved generation", async () => {
  const first = renderHook(() => useReaderNotebook("paper-1", sourceRevision))
  await waitFor(() => expect(first.result.current.ready).toBe(true))
  act(() => first.result.current.update({ note: "queued and awaiting receipt" }))
  const generation = history.historyState().generation
  expect(first.result.current.dirty).toBe(true)
  first.unmount()
  const second = renderHook(() => useReaderNotebook("paper-1", sourceRevision))
  await waitFor(() => expect(second.result.current.ready).toBe(true))
  expect(history.historyState().generation).toBe(generation)
  expect(second.result.current.note).toBe("queued and awaiting receipt")
  expect(second.result.current.dirty).toBe(true)
  expect(second.result.current.saveStatus).toBe("saving")
})


it("rejects retry in the restore notification turn before its effect can mark the conflict", async () => {
  const view = renderHook(() => useReaderNotebook("paper-1", sourceRevision))
  await waitFor(() => expect(view.result.current.ready).toBe(true))
  act(() => view.result.current.update({ note: "local edit before restore" }))
  act(() => {
    history.acceptAccountHistory(workspace({ ...saved, note: "restored server version", updatedAt: 20 }), 8)
    const restoredGeneration = history.historyState().generation
    expect(view.result.current.retrySave()).toBe(false)
    expect(history.historyState().generation).toBe(restoredGeneration)
  })
  await waitFor(() => expect(view.result.current.saveStatus).toBe("conflict"))
  expect(view.result.current.note).toBe("local edit before restore")
  expect(history.historyState().workspace.readerNotebooks?.[0]?.note).toBe("restored server version")
})

it("does not mistake an identical restored notebook for an acknowledgement of its pending generation", async () => {
  const view = renderHook(() => useReaderNotebook("paper-1", sourceRevision))
  await waitFor(() => expect(view.result.current.ready).toBe(true))
  act(() => view.result.current.update({ note: "same text in restored notebook" }))
  const pendingNotebook = history.historyState().workspace.readerNotebooks?.[0]
  if (!pendingNotebook) throw new Error("pending notebook missing")
  act(() => history.acceptAccountHistory(workspace({ ...pendingNotebook }), 8))
  await waitFor(() => expect(view.result.current.saveStatus).toBe("conflict"))
  expect(view.result.current.note).toBe("same text in restored notebook")
  expect(view.result.current.dirty).toBe(true)
  act(() => { expect(view.result.current.retrySave()).toBe(false) })
})
