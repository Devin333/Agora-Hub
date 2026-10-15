import { describe, expect, it, vi } from "vitest"
import { RendererLifecycleBridge, requiresNativeQuitConfirmation, validateRendererState } from "../src/lifecycle-bridge"

function fakeWindow() {
  return {
    isDestroyed: vi.fn(() => false),
    webContents: {
      isDestroyed: vi.fn(() => false),
      send: vi.fn()
    }
  }
}

function sentRequest(window: ReturnType<typeof fakeWindow>) {
  return window.webContents.send.mock.calls[0][1] as { requestId: string; state: { revision: number; dirty: boolean; activeTaskCount: number } }
}

describe("renderer lifecycle bridge", () => {
  it("requires monotonic bounded renderer state", () => {
    expect(validateRendererState({ revision: 1, dirty: true, activeTaskCount: 2, summary: "editing note" }, 0)).toEqual({ revision: 1, dirty: true, activeTaskCount: 2, summary: "editing note" })
    expect(validateRendererState({ revision: 1, dirty: false, activeTaskCount: 0 }, 1)).toBeNull()
    expect(validateRendererState({ revision: 2, dirty: false, activeTaskCount: -1 }, 1)).toBeNull()
  })

  it("exposes dirty and active-task state for controlled exit", () => {
    const bridge = new RendererLifecycleBridge()
    expect(bridge.needsConfirmation()).toBe(false)
    expect(bridge.update({ revision: 1, dirty: true, activeTaskCount: 0 })).toEqual({ accepted: true, revision: 1 })
    expect(bridge.needsConfirmation()).toBe(true)
    expect(bridge.update({ revision: 1, dirty: false, activeTaskCount: 0 })).toEqual({ accepted: false, revision: 1 })
  })

  it("does not trust an old clean snapshot when the fresh IPC request times out", async () => {
    vi.useFakeTimers()
    try {
      const bridge = new RendererLifecycleBridge()
      const window = fakeWindow()
      const pending = bridge.requestQuit(window as never, "window-close", 100)
      expect(window.webContents.send).toHaveBeenCalledWith("agora:lifecycle:quit-requested", expect.objectContaining({ reason: "window-close", state: { revision: 0, dirty: false, activeTaskCount: 0 } }))
      await vi.advanceTimersByTimeAsync(101)
      const assessment = await pending
      expect(assessment).toEqual({ status: "unknown", reason: "timeout", lastKnownState: { revision: 0, dirty: false, activeTaskCount: 0 } })
      expect(requiresNativeQuitConfirmation(assessment)).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it("treats an invalid reply as unknown even when the old snapshot is clean", async () => {
    const bridge = new RendererLifecycleBridge()
    const window = fakeWindow()
    const pending = bridge.requestQuit(window as never, "app-quit")
    const request = sentRequest(window)
    expect(bridge.respond({ requestId: request.requestId, decision: "allow", state: { revision: 1, dirty: false, activeTaskCount: 0 } })).toEqual({ accepted: false })
    const assessment = await pending
    expect(assessment).toMatchObject({ status: "unknown", reason: "invalid-response" })
    expect(requiresNativeQuitConfirmation(assessment)).toBe(true)
  })

  it("treats renderer crash as unknown and resolves a pending quit immediately", async () => {
    const bridge = new RendererLifecycleBridge()
    const window = fakeWindow()
    const pending = bridge.requestQuit(window as never, "window-close")
    bridge.rendererGone()
    const assessment = await pending
    expect(assessment).toEqual({ status: "unknown", reason: "renderer-gone", lastKnownState: { revision: 0, dirty: false, activeTaskCount: 0 } })
    expect(requiresNativeQuitConfirmation(assessment)).toBe(true)
  })

  it("allows prompt-free close only after a fresh valid response matching current revision and state", async () => {
    const bridge = new RendererLifecycleBridge()
    expect(bridge.update({ revision: 1, dirty: false, activeTaskCount: 0 })).toEqual({ accepted: true, revision: 1 })
    const window = fakeWindow()
    const pending = bridge.requestQuit(window as never, "window-close")
    const request = sentRequest(window)
    expect(bridge.respond({ requestId: request.requestId, decision: "allow", state: request.state })).toEqual({ accepted: true })
    const assessment = await pending
    expect(assessment).toEqual({ status: "confirmed", decision: "allow", state: request.state })
    expect(requiresNativeQuitConfirmation(assessment)).toBe(false)
  })
})
