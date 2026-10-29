import { describe, expect, it } from "vitest"
import { quitDecision, type RendererLifecycleState } from "./desktop-lifecycle"

const state = (patch: Partial<RendererLifecycleState> = {}): RendererLifecycleState => ({ revision: 3, dirty: false, activeTaskCount: 0, ...patch })

describe("desktop lifecycle quit decision", () => {
  it("allows a clean idle renderer without prompting", () => {
    let prompted = false
    expect(quitDecision(state(), () => { prompted = true; return false })).toBe("allow")
    expect(prompted).toBe(false)
  })

  it("cancels quit when the user keeps an unsaved draft", () => {
    expect(quitDecision(state({ dirty: true }), () => false)).toBe("cancel")
  })

  it("leaves only after explicit confirmation and says interrupted requests are not replayed", () => {
    let message = ""
    expect(quitDecision(state({ activeTaskCount: 2 }), value => { message = value; return true })).toBe("allow")
    expect(message).toContain("进行中的请求会被中断")
    expect(message).toContain("不会自动重放")
  })

  it("asks about changing the server when the host is about to leave this server", () => {
    let message = ""
    expect(quitDecision(state({ dirty: true }), value => { message = value; return false }, "server-change")).toBe("cancel")
    expect(message).toContain("未保存的草稿会丢失")
    expect(message).toContain("仍要更换服务器地址吗")
  })

  it("words a window close as closing rather than quitting", () => {
    let message = ""
    expect(quitDecision(state({ dirty: true, activeTaskCount: 1 }), value => { message = value; return false }, "window-close")).toBe("cancel")
    expect(message).toContain("仍有未保存草稿")
    expect(message).toContain("仍有 1 个活动请求")
    expect(message).toContain("仍要关闭吗")
    expect(message).not.toContain("仍要退出吗")
  })
})
