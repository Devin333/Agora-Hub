import { beforeEach, afterEach, describe, expect, it, vi } from "vitest"
import { prepareResearchResume, readResearchHistory, readResearchWorkspace, recordResearchVisit, removeResearchVisit, researchHistoryKey, researchHistoryLegacyKey, takeResearchResume, saveResearchGroup, updateResearchVisit, removeResearchGroup, restoreResearchVisit, selectHistoryOwner, historyState, acceptAccountHistory, importGuestHistory } from "./history"
import { researchQuestionHref } from "./entry"

describe("browser research history", () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear(); selectHistoryOwner(null) })
  afterEach(() => vi.restoreAllMocks())
  it("updates one question with real filters and scroll, then restores it once", () => {
    const href = researchQuestionHref("papers", "找 Agent 论文")
    recordResearchVisit(href)
    recordResearchVisit(href + "&sort=citations&page=2", 540)
    const [visit] = readResearchHistory()
    expect(readResearchHistory()).toHaveLength(1)
    expect(visit.href).toContain("sort=citations&page=2")
    expect(visit.scrollY).toBe(540)
    prepareResearchResume(visit)
    expect(takeResearchResume(visit.href)).toEqual(visit)
    expect(takeResearchResume(visit.href)).toBeNull()
  })
  it("retains more than ten studies and supports soft deletion and clear", () => {
    for (let i = 0; i < 15; i++) recordResearchVisit(researchQuestionHref("projects", `项目 ${i}`))
    expect(readResearchHistory()).toHaveLength(15)
    removeResearchVisit(readResearchHistory()[0].id)
    expect(readResearchHistory()).toHaveLength(14)
    removeResearchVisit()
    expect(readResearchHistory()).toEqual([])
  })
  it("rejects malformed, mismatched and external data", () => {
    localStorage.setItem(researchHistoryKey, "broken")
    expect(readResearchHistory()).toEqual([])
    recordResearchVisit(researchQuestionHref("papers", "Agent"))
    const good = readResearchHistory()[0]
    for (const invalid of [{ ...good, href: "//evil.test" }, { ...good, module: "reports" }, { ...good, question: "changed" }, { ...good, scrollY: -1 }]) {
      localStorage.setItem(researchHistoryKey, JSON.stringify({ visits: [invalid], groups: [] }))
      expect(readResearchHistory()).toEqual([])
    }
    recordResearchVisit("/projects")
    expect(readResearchHistory()).toEqual([])
  })
  it("remains usable when storage is blocked", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked") })
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked") })
    expect(() => recordResearchVisit(researchQuestionHref("community", "AI 讨论"))).not.toThrow()
    expect(readResearchHistory()).toEqual([])
    expect(historyState().status).toBe("error")
  })
  it("migrates v1 without removing it, then preserves metadata on filter updates", () => {
    const href = researchQuestionHref("papers", "Agent")
    const legacy = JSON.stringify([{ id: "papers:Agent", module: "papers", question: "Agent", href, scrollY: 510, updatedAt: 1000 }])
    localStorage.setItem(researchHistoryLegacyKey, legacy)
    const [visit] = readResearchHistory()
    expect(visit.scrollY).toBe(510)
    const groupId = saveResearchGroup("Agent 调研")!
    updateResearchVisit(visit.id, { title: "长上下文", isFavorite: true, groupId })
    recordResearchVisit(href + "&has=code", 800)
    expect(readResearchHistory()[0]).toMatchObject({ id: visit.id, title: "长上下文", isFavorite: true, groupId, scrollY: 800 })
    expect(localStorage.getItem(researchHistoryLegacyKey)).toBe(legacy)
  })
  it("keeps identical questions in independent sessions and never revives a deleted one through tracking", () => {
    recordResearchVisit(researchQuestionHref("papers", "Agent", "session-one"))
    recordResearchVisit(researchQuestionHref("papers", "Agent", "session-two"))
    recordResearchVisit(researchQuestionHref("papers", "Agent", "session-one") + "&page=2", 900)
    expect(readResearchHistory()).toHaveLength(2)
    expect(readResearchHistory().find(v => v.id === "session-one")?.scrollY).toBe(900)
    removeResearchVisit("session-one")
    recordResearchVisit(researchQuestionHref("papers", "Agent", "session-one"))
    expect(readResearchHistory()).toHaveLength(1)
    restoreResearchVisit("session-one")
    expect(readResearchHistory()).toHaveLength(2)
  })
  it("ungroups records on group deletion and keeps favorites through undo", () => {
    recordResearchVisit(researchQuestionHref("projects", "Agent", "study"))
    const groupId = saveResearchGroup("资料")!
    updateResearchVisit("study", { groupId, isFavorite: true })
    removeResearchVisit("study")
    removeResearchGroup(groupId)
    restoreResearchVisit("study")
    expect(readResearchHistory()[0]).toMatchObject({ groupId: null, isFavorite: true })
    expect(readResearchWorkspace().groups).toEqual([])
  })
  it("isolates account data and imports guest records explicitly and idempotently", () => {
    recordResearchVisit(researchQuestionHref("papers", "local", "guest-study"))
    selectHistoryOwner("alice")
    expect(readResearchHistory()).toEqual([])
    acceptAccountHistory({ visits: [], groups: [] }, 0)
    importGuestHistory(); importGuestHistory()
    expect(readResearchHistory()).toHaveLength(1)
    selectHistoryOwner("bob")
    expect(readResearchHistory()).toEqual([])
    selectHistoryOwner(null)
    expect(readResearchHistory()[0].id).toBe("guest-study")
  })
})
