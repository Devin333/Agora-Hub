import path from "node:path"
import { describe, expect, it, vi } from "vitest"
import { DATA_CLEAR_CONFIRMATION_TEXT, type RuntimeCliResponse } from "../src/contracts"
import { OwnedDataClearService } from "../src/owned-data-clear"
import type { RuntimeClient } from "../src/runtime-client"

const dataRoot = "C:\\Users\\tester\\AppData\\Roaming\\Agora\\runtime"
const profilePath = path.resolve(dataRoot, "..")

function response(command: RuntimeCliResponse["command"], state: string, data: Record<string, unknown>, ok = true): RuntimeCliResponse {
  return { ok, command, state, code: `desktop_${command.replaceAll("-", "_")}`, message: `${command} ${state}`, data }
}

function previewResponse(privateId = "python-private-preview") {
  return response("data-clear-preview", "CONFIRMATION_REQUIRED", {
    previewId: privateId,
    expiresAt: new Date(Date.now() + 300_000).toISOString(),
    stopped: true,
    scope: {
      profilePath,
      fileCount: 12,
      totalBytes: 3456,
      includes: ["runtime data", "browser sessions"],
      preserves: ["external backups", "other profiles"],
      credentialPolicy: "delete-only-this-profile-fixed-slot-without-reading-its-value",
      volatileClasses: ["not part of the public bridge"]
    }
  })
}

function client(overrides: { invoke?: ReturnType<typeof vi.fn>; invokePrivate?: ReturnType<typeof vi.fn> } = {}) {
  return {
    layout: { dataRoot },
    invoke: overrides.invoke ?? vi.fn().mockResolvedValue(previewResponse()),
    invokePrivate: overrides.invokePrivate ?? vi.fn()
  } as unknown as RuntimeClient
}

function lifecycle() {
  return vi.fn().mockResolvedValue(true)
}

describe("OwnedDataClearService", () => {
  it("projects a public preview id while keeping the owner capability private", async () => {
    const runtime = client()
    const service = new OwnedDataClearService(runtime, vi.fn(), vi.fn(), 42)
    const preview = await service.preview()

    expect(preview).toMatchObject({ ok: true, state: "CONFIRMATION_REQUIRED", stopped: true, scope: { profilePath, fileCount: 12, totalBytes: 3456 } })
    expect(preview.previewId).toMatch(/^[0-9a-f-]{36}$/i)
    expect(preview.previewId).not.toBe("python-private-preview")
    expect(preview.scope).not.toHaveProperty("volatileClasses")
  })

  it("rejects anything except the exact frozen Chinese phrase before native or owner authorization", async () => {
    const runtime = client()
    const readIdentity = vi.fn()
    const prepareExit = vi.fn()
    const service = new OwnedDataClearService(runtime, readIdentity, prepareExit, 42)
    const preview = await service.preview()
    const fresh = lifecycle()
    const confirm = vi.fn().mockResolvedValue(true)

    const result = await service.apply({ previewId: preview.previewId, confirmationText: `${DATA_CLEAR_CONFIRMATION_TEXT} ` }, fresh, confirm)
    expect(result).toMatchObject({ ok: false, state: "REJECTED", code: "desktop_data_clear_confirmation_mismatch" })
    expect(fresh).not.toHaveBeenCalled()
    expect(confirm).not.toHaveBeenCalled()
    expect(readIdentity).not.toHaveBeenCalled()
    expect(runtime.invokePrivate).not.toHaveBeenCalled()
  })

  it("uses fresh lifecycle twice, defaults cancellation to no apply, and preserves the preview", async () => {
    const runtime = client()
    const service = new OwnedDataClearService(runtime, vi.fn(), vi.fn(), 42)
    const preview = await service.preview()
    const fresh = lifecycle()
    const confirm = vi.fn().mockResolvedValue(false)

    await expect(service.apply({ previewId: preview.previewId, confirmationText: DATA_CLEAR_CONFIRMATION_TEXT }, fresh, confirm)).resolves.toMatchObject({ ok: false, state: "CANCELLED", cancelled: true })
    expect(fresh).toHaveBeenCalledOnce()
    expect(runtime.invokePrivate).not.toHaveBeenCalled()
  })

  it("sends private capability and exact Electron birth identity only over bounded stdin", async () => {
    const invokePrivate = vi.fn().mockResolvedValue(response("data-clear-apply", "PREPARED", {
      operationId: "a".repeat(32),
      exitRequired: true,
      finalizerCapability: "main-process-finalizer-secret"
    }))
    const runtime = client({ invokePrivate })
    const prepareExit = vi.fn().mockResolvedValue(true)
    const service = new OwnedDataClearService(runtime, vi.fn().mockResolvedValue({ pid: 42, creationTime: "133801234567890000" }), prepareExit, 42)
    const preview = await service.preview()
    const fresh = lifecycle()

    const result = await service.apply({ previewId: preview.previewId, confirmationText: DATA_CLEAR_CONFIRMATION_TEXT }, fresh, vi.fn().mockResolvedValue(true))
    expect(result).toEqual({ ok: true, state: "PREPARED", code: "desktop_data_clear_apply", message: "data-clear-apply PREPARED", operationId: "a".repeat(32), exitRequired: true })
    expect(result.state).not.toBe("CLEARED")
    expect(invokePrivate).toHaveBeenCalledWith("data-clear-apply", {
      version: 1,
      previewId: "python-private-preview",
      confirmationText: DATA_CLEAR_CONFIRMATION_TEXT,
      electron: { pid: 42, creationTime: "133801234567890000" }
    })
    expect(fresh).toHaveBeenCalledTimes(2)
    const finalizer = prepareExit.mock.calls[0]?.[0]
    expect(finalizer).toMatchObject({ version: 1, operationId: "a".repeat(32), finalizerCapability: "main-process-finalizer-secret", electron: { pid: 42, creationTime: "133801234567890000" } })
    expect(finalizer.readyNonce).toMatch(/^[A-Za-z0-9_-]+$/)
    expect(service.businessAdmissionClosed).toBe(true)
  })

  it("keeps PREPARED truthful and startup admission closed when host exit preparation fails", async () => {
    const runtime = client({ invokePrivate: vi.fn().mockResolvedValue(response("data-clear-apply", "PREPARED", { operationId: "b".repeat(32), exitRequired: true, finalizerCapability: "secret" })) })
    const service = new OwnedDataClearService(runtime, vi.fn().mockResolvedValue({ pid: 7, creationTime: "123" }), vi.fn().mockResolvedValue(false), 7)
    const preview = await service.preview()
    const result = await service.apply({ previewId: preview.previewId, confirmationText: DATA_CLEAR_CONFIRMATION_TEXT }, lifecycle(), vi.fn().mockResolvedValue(true))
    expect(result).toMatchObject({ ok: false, state: "PREPARED", code: "desktop_data_clear_host_exit_not_ready", exitRequired: true })
    expect(result.state).not.toBe("CLEARED")
    expect(service.businessAdmissionClosed).toBe(true)
  })

  it("rejects CLEARED without the finalizer operation identity and keeps startup fenced", async () => {
    const invoke = vi.fn().mockResolvedValue(response("data-clear-status", "CLEARED", { completedAt: "2026-10-15T00:00:00Z" }))
    const service = new OwnedDataClearService(client({ invoke }), vi.fn(), vi.fn(), 1)
    await expect(service.status()).resolves.toMatchObject({ ok: false, state: "UNKNOWN", code: "desktop_data_clear_status_protocol_invalid" })
    expect(service.businessAdmissionClosed).toBe(true)
  })
  it("keeps a first-start status transport failure non-destructive and closable", async () => {
    const invoke = vi.fn().mockRejectedValue(new Error("status unavailable"))
    const service = new OwnedDataClearService(client({ invoke }), vi.fn(), vi.fn(), 1)

    await expect(service.status()).resolves.toMatchObject({ ok: false, state: "UNKNOWN" })
    expect(service.businessAdmissionClosed).toBe(true)
    expect(service.destructiveExitPending).toBe(false)
  })
  it("does not let a later IDLE status silently reopen the already-fenced renderer", async () => {
    const invoke = vi.fn()
      .mockRejectedValueOnce(new Error("status unavailable"))
      .mockResolvedValueOnce(response("data-clear-status", "IDLE", {}))
    const service = new OwnedDataClearService(client({ invoke }), vi.fn(), vi.fn(), 1)

    await service.status()
    await expect(service.status()).resolves.toMatchObject({ ok: true, state: "IDLE" })
    expect(service.businessAdmissionClosed).toBe(true)
    expect(service.destructiveExitPending).toBe(false)
  })

  it("allows a fresh service instance to bootstrap from an owner-confirmed IDLE status", async () => {
    const invoke = vi.fn().mockResolvedValue(response("data-clear-status", "IDLE", {}))
    const service = new OwnedDataClearService(client({ invoke }), vi.fn(), vi.fn(), 1)

    await expect(service.status()).resolves.toMatchObject({ ok: true, state: "IDLE" })
    expect(service.businessAdmissionClosed).toBe(false)
    expect(service.destructiveExitPending).toBe(false)
  })
  it("keeps retry PREVIEW status projected as FAILED_PARTIAL while allowing explicit re-preview", async () => {
    const invoke = vi.fn()
      .mockResolvedValueOnce(response("data-clear-status", "FAILED_PARTIAL", { operationId: "e".repeat(32) }))
      .mockResolvedValueOnce(previewResponse("retry-private-preview"))
    const service = new OwnedDataClearService(client({ invoke }), vi.fn(), vi.fn(), 1)
    await service.status()
    expect(service.businessAdmissionClosed).toBe(true)
    await expect(service.preview()).resolves.toMatchObject({ ok: true, state: "CONFIRMATION_REQUIRED" })
    expect(invoke).toHaveBeenLastCalledWith("data-clear-preview")
    expect(service.businessAdmissionClosed).toBe(true)
  })
  it("allows atomic PREPARED re-preview while business admission stays closed", async () => {
    const invoke = vi.fn()
      .mockResolvedValueOnce(response("data-clear-status", "PREPARED", { operationId: "f".repeat(32) }))
      .mockResolvedValueOnce(previewResponse("replacement-private-preview"))
    const service = new OwnedDataClearService(client({ invoke }), vi.fn(), vi.fn(), 1)

    await service.status()
    expect(service.businessAdmissionClosed).toBe(true)
    expect(service.destructiveExitPending).toBe(true)
    await expect(service.preview()).resolves.toMatchObject({ ok: true, state: "CONFIRMATION_REQUIRED" })
    expect(service.businessAdmissionClosed).toBe(true)
    expect(service.destructiveExitPending).toBe(false)
  })
  it("accepts CLEARED only from status with a finalizer completion timestamp", async () => {
    const invoke = vi.fn()
      .mockResolvedValueOnce(response("data-clear-status", "FAILED_PARTIAL", { operationId: "c".repeat(32) }))
      .mockResolvedValueOnce(response("data-clear-status", "CLEARED", { operationId: "c".repeat(32), completedAt: "2026-10-15T00:00:00Z" }))
    const service = new OwnedDataClearService(client({ invoke }), vi.fn(), vi.fn(), 1)

    await expect(service.status()).resolves.toMatchObject({ state: "FAILED_PARTIAL", operationId: "c".repeat(32) })
    expect(service.businessAdmissionClosed).toBe(true)
    await expect(service.status()).resolves.toMatchObject({ ok: true, state: "CLEARED", completedAt: "2026-10-15T00:00:00Z" })
    expect(service.businessAdmissionClosed).toBe(false)
  })
})








