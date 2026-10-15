import { beforeEach, describe, expect, it, vi } from "vitest"

const electron = vi.hoisted(() => {
  const handlers = new Map<string, (...args: unknown[]) => unknown>()
  const parent = {
    isDestroyed: vi.fn(() => false),
    getNativeWindowHandle: vi.fn(() => Buffer.from([42, 0, 0, 0]))
  }
  return {
    handlers,
    parent,
    showMessageBox: vi.fn(),
    handle: vi.fn((channel: string, handler: (...args: unknown[]) => unknown) => handlers.set(channel, handler)),
    removeHandler: vi.fn((channel: string) => handlers.delete(channel))
  }
})

vi.mock("electron", () => ({
  BrowserWindow: { fromWebContents: vi.fn(() => electron.parent) },
  dialog: {
    showMessageBox: electron.showMessageBox,
    showSaveDialog: vi.fn(),
    showOpenDialog: vi.fn()
  },
  ipcMain: { handle: electron.handle, removeHandler: electron.removeHandler }
}))

import { IPC_CHANNELS, registerIpcHandlers } from "../src/ipc"

function event(url = "http://127.0.0.1:3120/paper-agent") {
  return {
    senderFrame: { url, processId: 10, routingId: 20, isDestroyed: () => false },
    sender: { id: 30, getURL: () => url }
  }
}

function preview() {
  return {
    ok: true,
    state: "CONFIRMATION_REQUIRED",
    code: "preview",
    message: "preview",
    previewId: "preview-id",
    expiresAt: "2026-10-14T10:00:00.000Z",
    current: { revision: 7 },
    configuration: {
      revision: 8,
      research: {
        llm: { model: "research-model", baseUrl: "https://api.example.test/v1" },
        source: { apiUrl: "https://export.arxiv.org/api/query" }
      }
    },
    effectScope: { llmConsumers: ["research"] }
  }
}

function options(overrides: Record<string, unknown> = {}) {
  const coordinator = {
    refresh: vi.fn().mockResolvedValue({ phase: "ready" }),
    retry: vi.fn(),
    stop: vi.fn(),
    diagnostics: {}
  }
  return {
    coordinator,
    lifecycle: { requestQuit: vi.fn(), update: vi.fn(), respond: vi.fn() },
    maintenance: { backup: vi.fn(), preview: vi.fn(), activate: vi.fn() },
    config: { show: vi.fn(), preview: vi.fn(), apply: vi.fn() },
    credentials: { invoke: vi.fn() },
    clearData: { preview: vi.fn(), apply: vi.fn(), status: vi.fn(), businessAdmissionClosed: false, destructiveExitPending: false },
    maintenanceFence: { active: false, enter: vi.fn(() => true), allowsIpc: vi.fn(() => true), isAuthorizedDocument: vi.fn(() => true) },
    startupFile: "C:\\Agora\\startup.html",
    getCanonicalOrigin: () => "http://127.0.0.1:3120",
    documentsPath: "C:\\Users\\tester\\Documents",
    logger: { write: vi.fn().mockResolvedValue(undefined) },
    onRuntimeReady: vi.fn(),
    ...overrides
  }
}

beforeEach(() => {
  electron.handlers.clear()
  electron.handle.mockClear()
  electron.removeHandler.mockClear()
  electron.showMessageBox.mockReset()
  electron.parent.isDestroyed.mockReturnValue(false)
})

describe("native IPC transaction boundaries", () => {
  it("does not stop or invoke owner apply when native lifecycle confirmation is cancelled", async () => {
    electron.showMessageBox.mockResolvedValueOnce({ response: 0 })
    const base = options()
    base.lifecycle.requestQuit.mockResolvedValue({ status: "confirmed", decision: "allow", state: { revision: 4, dirty: true, activeTaskCount: 0 } })
    base.config.apply.mockImplementation(async (_value, freshLifecycle) => {
      return await freshLifecycle()
        ? { ok: true, state: "APPLIED", code: "unexpected", message: "unexpected" }
        : { ok: false, state: "CANCELLED", code: "desktop_config_cancelled", message: "cancelled", cancelled: true }
    })
    registerIpcHandlers(base as never)

    const result = await electron.handlers.get(IPC_CHANNELS.configApply)?.(event(), { previewId: "opaque" })
    expect(result).toMatchObject({ ok: false, state: "CANCELLED", cancelled: true })
    expect(base.coordinator.stop).not.toHaveBeenCalled()
    expect(base.config.apply).toHaveBeenCalledOnce()
  })

  it("keeps dirty state authoritative and asks for a fresh lifecycle decision after config confirmation", async () => {
    electron.showMessageBox
      .mockResolvedValueOnce({ response: 1 })
      .mockResolvedValueOnce({ response: 1 })
      .mockResolvedValueOnce({ response: 1 })
    const base = options()
    const dirty = { status: "confirmed", decision: "allow", state: { revision: 9, dirty: true, activeTaskCount: 1 } }
    base.lifecycle.requestQuit.mockResolvedValue(dirty)
    base.config.apply.mockImplementation(async (_value, freshLifecycle, confirm) => {
      expect(await freshLifecycle()).toBe(true)
      expect(await confirm(preview())).toBe(true)
      expect(await freshLifecycle()).toBe(true)
      return { ok: true, state: "APPLIED", code: "applied", message: "applied" }
    })
    registerIpcHandlers(base as never)

    await expect(electron.handlers.get(IPC_CHANNELS.configApply)?.(event(), { previewId: "opaque" })).resolves.toMatchObject({ ok: true, state: "APPLIED" })
    expect(base.lifecycle.requestQuit).toHaveBeenCalledTimes(2)
    expect(base.lifecycle.update).not.toHaveBeenCalled()
    expect(electron.showMessageBox).toHaveBeenCalledTimes(3)
    expect(base.coordinator.stop).not.toHaveBeenCalled()
  })

  it("does not fabricate credential presence or stop runtime when native replace is cancelled", async () => {
    electron.showMessageBox.mockResolvedValueOnce({ response: 0 })
    const base = options()
    base.credentials.invoke.mockResolvedValue({ ok: true, code: "inspected", configured: false, state: "READY" })
    registerIpcHandlers(base as never)

    const result = await electron.handlers.get(IPC_CHANNELS.credentialReplace)?.(event())
    expect(result).toEqual({ ok: true, code: "credential_cancelled", configured: false, state: "READY", changed: false, cancelled: true })
    expect(base.credentials.invoke).toHaveBeenCalledWith("status")
    expect(base.credentials.invoke).not.toHaveBeenCalledWith("replace", expect.anything())
    expect(base.coordinator.stop).not.toHaveBeenCalled()
  })

  it("routes credential deletion through the owner command rather than coordinator or vault access", async () => {
    electron.showMessageBox.mockResolvedValueOnce({ response: 1 })
    const base = options()
    base.lifecycle.requestQuit.mockResolvedValue({ status: "confirmed", decision: "allow", state: { revision: 3, dirty: false, activeTaskCount: 0 } })
    base.credentials.invoke.mockResolvedValue({ ok: true, code: "deleted", configured: false, changed: true, state: "STOPPED" })
    registerIpcHandlers(base as never)

    await expect(electron.handlers.get(IPC_CHANNELS.credentialRemove)?.(event())).resolves.toMatchObject({ ok: true, configured: false, state: "STOPPED" })
    expect(base.credentials.invoke).toHaveBeenCalledWith("delete")
    expect(base.coordinator.stop).not.toHaveBeenCalled()
  })
  it("rejects a same-URL replacement document before clear preview", async () => {
    const fence = {
      active: false,
      enter: vi.fn(() => true),
      allowsIpc: vi.fn(() => true),
      isAuthorizedDocument: vi.fn(() => false)
    }
    const base = options({ maintenanceFence: fence })
    registerIpcHandlers(base as never)

    await expect(electron.handlers.get(IPC_CHANNELS.runtimeClearDataPreview)?.(event())).rejects.toThrow("ipc_clear_document_rejected")
    expect(base.clearData.preview).not.toHaveBeenCalled()
    expect(fence.enter).not.toHaveBeenCalled()
  })

  it("allows the exact owned startup file to enter clear maintenance", async () => {
    const startup = event("file:///C:/Agora/startup.html")
    const fence = {
      active: false,
      enter: vi.fn(() => true),
      allowsIpc: vi.fn(() => true),
      isAuthorizedDocument: vi.fn(() => false)
    }
    const base = options({ maintenanceFence: fence })
    base.clearData.preview.mockResolvedValue({ ok: false, state: "FAILED_PARTIAL", code: "blocked", message: "blocked" })
    registerIpcHandlers(base as never)

    await expect(electron.handlers.get(IPC_CHANNELS.runtimeClearDataPreview)?.(startup)).resolves.toMatchObject({ state: "FAILED_PARTIAL" })
    expect(fence.enter).toHaveBeenCalledWith(startup)
  })
  it("keeps phrase mismatch before lifecycle and native destructive confirmation", async () => {
    const base = options()
    base.clearData.apply.mockResolvedValue({ ok: false, state: "REJECTED", code: "desktop_data_clear_confirmation_mismatch", message: "mismatch" })
    registerIpcHandlers(base as never)

    await expect(electron.handlers.get(IPC_CHANNELS.runtimeClearDataApply)?.(event(), { previewId: "id", confirmationText: "错误" })).resolves.toMatchObject({ state: "REJECTED" })
    expect(base.lifecycle.requestQuit).not.toHaveBeenCalled()
    expect(electron.showMessageBox).not.toHaveBeenCalled()
  })

  it("uses a separate default-cancel native dialog before the owner apply can prepare exit", async () => {
    electron.showMessageBox.mockResolvedValueOnce({ response: 0 })
    const base = options()
    base.lifecycle.requestQuit.mockResolvedValue({ status: "confirmed", decision: "allow", state: { revision: 5, dirty: false, activeTaskCount: 0 } })
    base.clearData.apply.mockImplementation(async (_value, freshLifecycle, confirm) => {
      if (!await freshLifecycle()) return { ok: false, state: "CANCELLED", code: "cancelled", message: "cancelled", cancelled: true }
      const accepted = await confirm({
        ok: true, state: "CONFIRMATION_REQUIRED", code: "preview", message: "preview", previewId: "public", expiresAt: "2026-10-15T05:00:00Z", stopped: true,
        scope: { profilePath: "C:\\Users\\tester\\Agora", fileCount: 3, totalBytes: 4, includes: ["runtime"], preserves: ["backup"], credentialPolicy: "fixed-slot" }
      })
      return accepted ? { ok: true, state: "PREPARED", code: "prepared", message: "prepared", exitRequired: true } : { ok: false, state: "CANCELLED", code: "cancelled", message: "cancelled", cancelled: true }
    })
    registerIpcHandlers(base as never)

    await expect(electron.handlers.get(IPC_CHANNELS.runtimeClearDataApply)?.(event(), { previewId: "public", confirmationText: "删除本机全部 Agora 数据" })).resolves.toMatchObject({ state: "CANCELLED", cancelled: true })
    expect(electron.showMessageBox).toHaveBeenCalledWith(electron.parent, expect.objectContaining({ buttons: ["取消", "清除并退出"], defaultId: 0, cancelId: 0 }))
  })

  it("allows lifecycle IPC only for the exact fenced owned startup document", async () => {
    const startup = event("file:///C:/Agora/startup.html")
    const fence = {
      active: true,
      enter: vi.fn(() => true),
      allowsIpc: vi.fn((_event: unknown, channel: string) => channel === IPC_CHANNELS.lifecycleUpdate || channel === IPC_CHANNELS.lifecycleRespond)
    }
    const base = options({ maintenanceFence: fence })
    base.lifecycle.update.mockReturnValue({ ok: true })
    base.lifecycle.respond.mockReturnValue(true)
    registerIpcHandlers(base as never)

    expect(electron.handlers.get(IPC_CHANNELS.lifecycleUpdate)?.(startup, { revision: 1, dirty: false, activeTaskCount: 0 })).toEqual({ ok: true })
    expect(electron.handlers.get(IPC_CHANNELS.lifecycleRespond)?.(startup, { requestId: "q", decision: "allow", state: { revision: 1, dirty: false, activeTaskCount: 0 } })).toBe(true)
    expect(fence.allowsIpc).toHaveBeenCalledWith(startup, IPC_CHANNELS.lifecycleUpdate)
    expect(fence.allowsIpc).toHaveBeenCalledWith(startup, IPC_CHANNELS.lifecycleRespond)
  })
})





