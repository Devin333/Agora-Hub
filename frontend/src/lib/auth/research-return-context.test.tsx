import { act, cleanup, renderHook, waitFor } from "@testing-library/react"
import { StrictMode, useEffect, useState } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { preserveResearchForRedirect, useResearchReturnContext } from "./research-return-context"

describe("research context across a full-page OAuth fallback", () => {
  beforeEach(() => {
    sessionStorage.clear()
    window.history.replaceState({}, "", "/design-demo/papers?q=Agent")
    vi.spyOn(window, "scrollTo").mockImplementation(() => {})
  })
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it("restores the latest unsubmitted draft once without adding login data", async () => {
    const restore = vi.fn()
    const initial = renderHook(({ draft }) => useResearchReturnContext("papers", draft, restore), { initialProps: { draft: "old" } })
    initial.rerender({ draft: "尚未提交的研究问题" })
    expect(sessionStorage.length).toBe(0)
    act(() => preserveResearchForRedirect())
    const saved = JSON.parse(sessionStorage.getItem("agora-auth-return:papers")!)
    expect(Object.keys(saved).sort()).toEqual(["createdAt", "path", "scrollY", "value"])
    expect(saved.value).toBe("尚未提交的研究问题")
    initial.unmount()
    renderHook(() => useResearchReturnContext("papers", "", restore))
    await waitFor(() => expect(restore).toHaveBeenCalledExactlyOnceWith("尚未提交的研究问题"))
    expect(sessionStorage.length).toBe(0)
  })

  it("restores after page initialization in StrictMode without consuming the draft early", async () => {
    sessionStorage.setItem("agora-auth-return:papers", JSON.stringify({ path: window.location.pathname + window.location.search, createdAt: Date.now(), value: "未提交的问题" }))
    const { result } = renderHook(() => {
      const [draft, setDraft] = useState("")
      useEffect(() => setDraft("Agent"), [])
      useResearchReturnContext("papers", draft, setDraft)
      return draft
    }, { wrapper: StrictMode })
    await waitFor(() => expect(result.current).toBe("未提交的问题"))
    expect(sessionStorage.length).toBe(0)
  })

  it.each([
    { path: "/design-demo", age: 0 },
    { path: "/design-demo/papers?q=other", age: 0 },
    { path: "/design-demo/papers?q=Agent", age: 900001 },
    { path: "/design-demo/papers?q=Agent", age: -1000 },
  ])("discards stale or different-page drafts: $path / $age", ({ path, age }) => {
    sessionStorage.setItem("agora-auth-return:papers", JSON.stringify({ path, createdAt: Date.now() - age, value: "private draft" }))
    const restore = vi.fn()
    renderHook(() => useResearchReturnContext("papers", "", restore))
    expect(restore).not.toHaveBeenCalled()
    expect(sessionStorage.length).toBe(0)
  })
})
