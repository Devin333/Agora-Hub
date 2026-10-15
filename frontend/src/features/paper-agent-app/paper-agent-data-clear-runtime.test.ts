import { afterEach, describe, expect, it, vi } from "vitest"
import {
  applyDesktopDataClear,
  DATA_CLEAR_CONFIRMATION_TEXT,
  hasDesktopDataClearBridge,
  previewDesktopDataClear,
  readDesktopDataClearStatus,
} from "./paper-agent-data-clear-runtime"

const scope = {
  profilePath: "C:/Users/test/AppData/Roaming/Agora",
  fileCount: 12,
  totalBytes: 4096,
  includes: ["账号与会话", "论文资料、笔记与成果", "个人记忆、配置、缓存和日志"],
  preserves: ["安装资源", "原始导入文件", "外部备份与导出", "其他 profile"],
  credentialPolicy: "只删除当前 profile 的专属系统凭据槽位。",
}

function installBridge(overrides: Partial<{ preview: ReturnType<typeof vi.fn>; apply: ReturnType<typeof vi.fn>; status: ReturnType<typeof vi.fn> }> = {}) {
  const preview = overrides.preview ?? vi.fn().mockResolvedValue({ ok: true, state: "CONFIRMATION_REQUIRED", code: "data_clear_preview_ready", message: "ready", previewId: "preview-1", expiresAt: "2099-10-15T08:00:00.000Z", scope, stopped: true })
  const apply = overrides.apply ?? vi.fn().mockResolvedValue({ ok: true, state: "PREPARED", code: "data_clear_prepared", message: "prepared", operationId: "operation-1", exitRequired: true })
  const status = overrides.status ?? vi.fn().mockResolvedValue({ ok: true, state: "IDLE", code: "data_clear_idle", message: "idle" })
  Object.assign(window, { agoraDesktop: { version: 1, runtime: { clearData: { preview, apply, status } } } })
  return { preview, apply, status }
}

afterEach(() => {
  delete (window as unknown as { agoraDesktop?: unknown }).agoraDesktop
  vi.restoreAllMocks()
})

describe("paper agent owned data clear bridge", () => {
  it("is visibly detectable only when every frozen clearData method exists", () => {
    expect(hasDesktopDataClearBridge()).toBe(false)
    Object.assign(window, { agoraDesktop: { version: 1, runtime: { clearData: { preview: vi.fn(), apply: vi.fn() } } } })
    expect(hasDesktopDataClearBridge()).toBe(false)
    installBridge()
    expect(hasDesktopDataClearBridge()).toBe(true)
  })

  it("parses the real scope and sends only the opaque preview plus exact phrase", async () => {
    const bridge = installBridge()

    await expect(previewDesktopDataClear()).resolves.toMatchObject({ stopped: true, scope: { fileCount: 12, credentialPolicy: expect.any(String) } })
    await expect(applyDesktopDataClear("preview-1", DATA_CLEAR_CONFIRMATION_TEXT)).resolves.toMatchObject({ state: "PREPARED", exitRequired: true })
    await expect(readDesktopDataClearStatus()).resolves.toMatchObject({ state: "IDLE" })

    expect(bridge.apply).toHaveBeenCalledWith({ previewId: "preview-1", confirmationText: DATA_CLEAR_CONFIRMATION_TEXT })
    const serialized = JSON.stringify(bridge.apply.mock.calls[0]?.[0])
    expect(serialized).not.toMatch(/token|credential|secret|path|command|target/i)
  })

  it("rejects a phrase mismatch before invoking the native bridge", async () => {
    const bridge = installBridge()
    await expect(applyDesktopDataClear("preview-1", `${DATA_CLEAR_CONFIRMATION_TEXT} `)).rejects.toThrow()
    expect(bridge.apply).not.toHaveBeenCalled()
  })

  it("rejects secret tokens, arbitrary paths, and unknown response fields", async () => {
    const tokenPreview = vi.fn().mockResolvedValue({ ok: true, state: "CONFIRMATION_REQUIRED", code: "ready", message: "ready", previewId: "preview-1", expiresAt: "2099-10-15T08:00:00.000Z", scope, stopped: true, token: "private-capability" })
    installBridge({ preview: tokenPreview })
    await expect(previewDesktopDataClear()).rejects.toThrow()

    const pathPreview = vi.fn().mockResolvedValue({ ok: true, state: "CONFIRMATION_REQUIRED", code: "ready", message: "ready", previewId: "preview-2", expiresAt: "2099-10-15T08:00:00.000Z", scope: { ...scope, arbitraryPath: "D:/delete-this" }, stopped: true })
    installBridge({ preview: pathPreview })
    await expect(previewDesktopDataClear()).rejects.toThrow()
  })


  it("rejects a complete successful preview with an unknown state", async () => {
    const preview = vi.fn().mockResolvedValue({ ok: true, state: "UNKNOWN", code: "ready", message: "ready", previewId: "preview-1", expiresAt: "2099-10-15T08:00:00.000Z", scope, stopped: true })
    installBridge({ preview })
    await expect(previewDesktopDataClear()).rejects.toThrow()
    expect(preview).toHaveBeenCalledTimes(1)
  })

  it("preserves false PREPARED and FAILED_PARTIAL responses without promoting them to success", async () => {
    const apply = vi.fn().mockResolvedValueOnce({ ok: false, state: "PREPARED", code: "desktop_data_clear_host_exit_not_ready", message: "host not ready", operationId: "operation-1", exitRequired: true })
      .mockResolvedValueOnce({ ok: false, state: "FAILED_PARTIAL", code: "desktop_data_clear_failed_partial", message: "partial", operationId: "operation-1" })
    installBridge({ apply })
    await expect(applyDesktopDataClear("preview-1", DATA_CLEAR_CONFIRMATION_TEXT)).resolves.toEqual({ ok: false, state: "PREPARED", code: "desktop_data_clear_host_exit_not_ready", message: "host not ready", operationId: "operation-1", exitRequired: true })
    await expect(applyDesktopDataClear("preview-2", DATA_CLEAR_CONFIRMATION_TEXT)).resolves.toEqual({ ok: false, state: "FAILED_PARTIAL", code: "desktop_data_clear_failed_partial", message: "partial", operationId: "operation-1" })
  })

  it("never accepts apply CLEARED or a PREPARED response without exit ownership", async () => {
    const apply = vi.fn().mockResolvedValue({ ok: true, state: "CLEARED", code: "bad", message: "bad", operationId: "operation-1", exitRequired: false })
    installBridge({ apply })
    await expect(applyDesktopDataClear("preview-1", DATA_CLEAR_CONFIRMATION_TEXT)).rejects.toThrow()

    apply.mockResolvedValueOnce({ ok: true, state: "PREPARED", code: "bad", message: "bad", operationId: "operation-2", exitRequired: false })
    await expect(applyDesktopDataClear("preview-1", DATA_CLEAR_CONFIRMATION_TEXT)).rejects.toThrow()
  })
})
