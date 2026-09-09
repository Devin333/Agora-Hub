import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { acceptAccountHistory, readResearchWorkspace, saveResearchMaterial, selectHistoryOwner, setHistoryStatus } from "./history"
import { emptyWorkspace } from "./history-model"
import { guidedMaterialContext, waitForGuidedMaterials } from "./guided-context"

const material = { id: "reference", kind: "note" as const, title: "研究笔记", notes: "研究任务完成情况", url: "", groupId: null, createdAt: 1, updatedAt: 1 }
beforeEach(() => { localStorage.clear(); selectHistoryOwner(null) })
afterEach(() => { selectHistoryOwner(null); vi.useRealTimers() })

it("sends guest reference content without pretending a local ID grants server access", () => {
  saveResearchMaterial(material)
  expect(guidedMaterialContext(null, [material.id])).toEqual({ materialIds: [], publicSources: [{ id: material.id, kind: "note", title: material.title, notes: material.notes, url: "" }] })
  expect(() => guidedMaterialContext(null, ["missing"])).toThrow("重新选择")
})

it("waits for account synchronization before sending private references", async () => {
  vi.useFakeTimers()
  selectHistoryOwner("alice"); acceptAccountHistory(emptyWorkspace(), 0); saveResearchMaterial(material, "alice")
  const finished = vi.fn()
  const wait = waitForGuidedMaterials("alice", [material.id], new AbortController().signal).then(finished)
  await vi.advanceTimersByTimeAsync(300); expect(finished).not.toHaveBeenCalled()
  acceptAccountHistory(readResearchWorkspace(), 1)
  await vi.advanceTimersByTimeAsync(100); await wait
  expect(guidedMaterialContext("alice", [material.id])).toEqual({ materialIds: [material.id] })
})

it("reports sync conflicts and allows cancellation rather than searching stale material IDs", async () => {
  selectHistoryOwner("alice"); acceptAccountHistory(emptyWorkspace(), 0); setHistoryStatus("conflict")
  await expect(waitForGuidedMaterials("alice", [material.id], new AbortController().signal)).rejects.toThrow("尚未同步")
  setHistoryStatus("synced")
  const controller = new AbortController(); controller.abort(new Error("cancelled"))
  await expect(waitForGuidedMaterials("alice", [material.id], controller.signal)).rejects.toThrow("cancelled")
})
