import { act, cleanup, render } from "@testing-library/react"
import { StrictMode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ResearchHistoryTracker } from "./research-history-tracker"
import { acceptAccountHistory, prepareResearchResume, readResearchHistory, readResearchWorkspace, recordResearchVisit, selectHistoryOwner, takeResearchResume } from "@/lib/research/history"
import { researchQuestionHref } from "@/lib/research/entry"

let path = "/design-demo/papers"
let query = ""
vi.mock("next/navigation", () => ({ usePathname: () => path, useSearchParams: () => new URLSearchParams(query) }))

describe("research continuity after asynchronous loading", () => {
  let resize: () => void
  let height: number
  beforeEach(() => {
    vi.useFakeTimers()
    localStorage.clear(); sessionStorage.clear()
    selectHistoryOwner(null)
    height = 900
    path = "/design-demo/papers"
    query = researchQuestionHref("papers", "Agent").split("?")[1] + "&sort=most_cited&page=2"
    vi.stubGlobal("ResizeObserver", class { constructor(callback: () => void) { resize = callback } observe() {} disconnect() {} })
    vi.spyOn(document.documentElement, "scrollHeight", "get").mockImplementation(() => height)
    vi.spyOn(window, "scrollTo").mockImplementation(() => {})
    Object.defineProperty(window, "scrollY", { configurable: true, value: 0, writable: true })
  })
  afterEach(() => { cleanup(); selectHistoryOwner(null); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
  it("retains filters and saved position across StrictMode while waiting for real content height", () => {
    const href = `${path}?${query}`
    recordResearchVisit(href, 600)
    prepareResearchResume(readResearchHistory()[0])
    render(<StrictMode><ResearchHistoryTracker /></StrictMode>)
    expect(window.scrollTo).not.toHaveBeenCalled()
    expect(readResearchHistory()[0].scrollY).toBe(600)
    act(() => { height = 2400; resize() })
    expect(window.scrollTo).toHaveBeenCalledWith({ top: 600, behavior: "instant" })
    expect(readResearchHistory()[0].href).toBe(href)
    act(() => { Object.defineProperty(window, "scrollY", { value: 420 }); window.dispatchEvent(new Event("scroll")); vi.advanceTimersByTime(200) })
    expect(readResearchHistory()[0].scrollY).toBe(420)
  })
  it("tracks filter updates and leaves the saved question intact on return home", () => {
    const view = render(<ResearchHistoryTracker />)
    query += "&has=code"
    view.rerender(<ResearchHistoryTracker />)
    expect(readResearchHistory()).toHaveLength(1)
    expect(readResearchHistory()[0].href).toContain("has=code")
    path = "/design-demo"; query = ""
    view.rerender(<ResearchHistoryTracker />)
    expect(readResearchHistory()[0].question).toBe("Agent")
  })
  it("does not carry an unfinished scroll restoration into a different account", () => {
    const href = `${path}?${query}`
    recordResearchVisit(href, 600)
    const saved = readResearchWorkspace()
    selectHistoryOwner("alice"); acceptAccountHistory(saved, 1)
    prepareResearchResume(readResearchHistory()[0])
    render(<ResearchHistoryTracker expectedOwner="alice" />)
    expect(window.scrollTo).not.toHaveBeenCalled()
    act(() => { selectHistoryOwner("bob") })
    expect(takeResearchResume(href)).toBeNull()
    expect(readResearchHistory()).toEqual([])
  })
})
