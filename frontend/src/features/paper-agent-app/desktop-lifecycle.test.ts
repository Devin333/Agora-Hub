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

  it("allows host drain only after explicit confirmation and never requests replay", () => {
    let message = ""
    expect(quitDecision(state({ activeTaskCount: 2 }), value => { message = value; return true })).toBe("allow")
    expect(message).toContain("等待当前请求安全结束")
    expect(message).toContain("不会自动重放")
  })

  it("uses the data-clear lifecycle reason without claiming preview or deletion completed", () => {
    let message = ""
    expect(quitDecision(state({ dirty: true, activeTaskCount: 1 }), value => { message = value; return false }, "data-clear")).toBe("cancel")
    expect(message).toContain("继续清除申请")
    expect(message).toContain("独立原生确认")
    expect(message).toContain("不代表数据已经清除")
    expect(message).not.toContain("预览已")
  })

  it("uses a configuration-specific stop message for config apply", () => {
    let message = ""
    expect(quitDecision(state({ activeTaskCount: 1 }), value => { message = value; return true }, "config-apply")).toBe("allow")
    expect(message).toContain("继续应用配置")
    expect(message).not.toContain("仍要退出吗")
  })
})
