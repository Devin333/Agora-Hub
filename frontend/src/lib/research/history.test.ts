import { beforeEach, afterEach, describe, expect, it, vi } from "vitest"
import { prepareResearchResume, readResearchHistory, recordResearchVisit, removeResearchVisit, researchHistoryKey, takeResearchResume } from "./history"
import { researchQuestionHref } from "./entry"

describe("browser research history", () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear() })
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
  it("bounds history and supports single deletion and clear", () => {
    for (let i = 0; i < 15; i++) recordResearchVisit(researchQuestionHref("projects", `项目 ${i}`))
    expect(readResearchHistory()).toHaveLength(10)
    removeResearchVisit(readResearchHistory()[0].id)
    expect(readResearchHistory()).toHaveLength(9)
    removeResearchVisit()
    expect(readResearchHistory()).toEqual([])
  })
  it("rejects malformed, mismatched and external data", () => {
    localStorage.setItem(researchHistoryKey, "broken")
    expect(readResearchHistory()).toEqual([])
    recordResearchVisit(researchQuestionHref("papers", "Agent"))
    const good = readResearchHistory()[0]
    localStorage.setItem(researchHistoryKey, JSON.stringify([{ ...good, href: "//evil.test" }, { ...good, module: "reports" }, { ...good, question: "changed" }, { ...good, scrollY: -1 }]))
    expect(readResearchHistory()).toEqual([])
    recordResearchVisit("/projects")
    expect(readResearchHistory()).toEqual([])
  })
  it("remains usable when storage is blocked", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked") })
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked") })
    expect(() => recordResearchVisit(researchQuestionHref("community", "AI 讨论"))).not.toThrow()
    expect(readResearchHistory()).toEqual([])
  })
})
