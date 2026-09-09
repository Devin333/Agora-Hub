import { beforeEach, describe, expect, it, vi } from "vitest"
import { paperQuestionHref, paperReaderHref, rememberPaperScroll, restorePaperScroll, safePaperReturnTo } from "../discovery-navigation"

describe("paper discovery navigation", () => {
  it("returns to the original guided research conversation", () => {
    const returnTo = "/design-demo?researchSession=owned-session"
    expect(safePaperReturnTo(returnTo)).toBe(returnTo)
    expect(new URL(paperReaderHref("paper", returnTo), "http://localhost").searchParams.get("returnTo")).toBe(returnTo)
  })
  beforeEach(() => sessionStorage.clear())
  it.each(["https://evil.test/papers", "//evil.test/papers", "/\\evil.test", "/login", "/papers/../login", "javascript:alert(1)"])("rejects unsafe destination %s", (value) => {
    expect(safePaperReturnTo(value)).toBe("/papers")
  })
  it("retains research filters while removing the preview state", () => {
    const path = "/design-demo/papers?q=Agent&topic=cs.AI&page=2&view=reading&paper=123"
    expect(safePaperReturnTo(path)).toBe("/design-demo/papers?q=Agent&topic=cs.AI&page=2&view=reading")
    const href = new URL(paperReaderHref("a/b", path), "http://localhost")
    expect(href.pathname).toBe("/design-demo/papers/a%2Fb/read")
    expect(href.searchParams.get("returnTo")).toBe(safePaperReturnTo(path))
  })
  it("retains the formal reader outside the design discovery surface", () => {
    expect(paperReaderHref("paper")).toBe("/papers/paper/read")
    expect(paperReaderHref("paper", "/papers?q=Agent")).toBe("/papers/paper/read?returnTo=%2Fpapers%3Fq%3DAgent")
  })
  it.each(["/papers/methods", "/papers/tasks"])("preserves category detail and search when returning to %s", (path) => {
    const returnTo = `${path}?q=Agent&group=agents&category=planning`
    expect(safePaperReturnTo(returnTo)).toBe(returnTo)
    const href = new URL(paperReaderHref("paper", returnTo), "http://localhost")
    expect(href.searchParams.get("returnTo")).toBe(returnTo)
    expect(safePaperReturnTo(`${path}/unapproved`)).toBe("/papers")
  })
  it("carries a home question verbatim without inventing a semantic query", () => {
    const question = "找出支持长上下文 Agent 评测的论文"
    const href = new URL(paperQuestionHref(question), "http://localhost")
    expect(href.searchParams.get("question")).toBe(question)
    expect(href.searchParams.get("q")).toBe(question)
  })
  it("restores scroll only for the corresponding list and only once", () => {
    vi.spyOn(window, "scrollY", "get").mockReturnValue(450)
    const scroll = vi.spyOn(window, "scrollTo").mockImplementation(() => undefined)
    rememberPaperScroll("/design-demo/papers?q=Agent&page=2")
    restorePaperScroll("/design-demo/papers")
    expect(scroll).not.toHaveBeenCalled()
    restorePaperScroll("/design-demo/papers?q=Agent&page=2")
    expect(scroll).toHaveBeenCalledWith({ top: 450, behavior: "instant" })
    restorePaperScroll("/design-demo/papers?q=Agent&page=2")
    expect(scroll).toHaveBeenCalledTimes(1)
    vi.restoreAllMocks()
  })
})
