import { describe, expect, it, vi } from "vitest"
import { RuntimeMaintenanceService } from "../src/runtime-maintenance"
import type { RuntimeStatusView } from "../src/contracts"

function status(phase: RuntimeStatusView["phase"]): RuntimeStatusView {
  return {
    phase,
    state: phase === "stopped" ? "STOPPED" : "READY",
    code: `runtime_${phase}`,
    message: phase,
    canonicalOrigin: phase === "ready" ? "http://127.0.0.1:3120" : null,
    instanceKey: phase === "ready" ? "owned-instance" : null,
    localTransportReady: phase === "ready",
    canRetry: false,
    checkedAt: new Date().toISOString()
  }
}

function previewResponse(withMemory = true) {
  return {
    ok: true,
    command: "restore" as const,
    state: "VERIFIED",
    code: "desktop_restore_verified",
    message: "verified",
    data: {
      backup_id: "backup-20261014-abcd1234",
      restore_root: "C:\\secret\\restore-root",
      validation: { application_isolation: { verified: true, configured_roots: { auth: "C:\\secret" } } },
      personal_memory_restore: withMemory ? {
        backup_id: "backup-20261014-abcd1234",
        confirmation_token: "restore-personal-memory:backup-20261014-abcd1234",
        status_counts: { active: 3, deleted: 1 },
        policy_count: 2,
        requires_explicit_activation_confirmation: true,
        warning: "Restores reviewed memory including tombstones."
      } : null
    }
  }
}

function snapshotStore() {
  const snapshot = { path: "C:\\private\\owned-snapshot.zip", sha256: "a".repeat(64), size: 123 }
  return {
    snapshot,
    store: {
      create: vi.fn().mockResolvedValue(snapshot),
      verify: vi.fn().mockResolvedValue(true),
      remove: vi.fn().mockResolvedValue(undefined)
    }
  }
}

describe("RuntimeMaintenanceService", () => {
  it("keeps paths and the personal-memory token in a TTL-bound main-process receipt", async () => {
    const invoke = vi.fn()
      .mockResolvedValueOnce(previewResponse())
      .mockResolvedValueOnce({ ok: true, command: "restore", state: "ACTIVATED", code: "desktop_restore_activated", message: "activated", data: { backup_id: "backup-20261014-abcd1234" } })
    const lifecycle = { status: status("stopped"), refresh: vi.fn().mockResolvedValue(status("stopped")), stop: vi.fn() }
    const snapshots = snapshotStore()
    const service = new RuntimeMaintenanceService({ invoke } as never, lifecycle, snapshots.store)

    const preview = await service.preview("C:\\private\\selected.zip")
    expect(preview).toMatchObject({ ok: true, state: "VERIFIED", backupId: "backup-20261014-abcd1234", isolationVerified: true, personalMemory: { statusCounts: { active: 3, deleted: 1 }, policyCount: 2 } })
    expect(JSON.stringify(preview)).not.toContain("confirmation_token")
    expect(JSON.stringify(preview)).not.toContain("selected.zip")
    expect(JSON.stringify(preview)).not.toContain("restore-root")

    const confirm = vi.fn().mockResolvedValue(true)
    const activated = await service.activate({ previewId: preview.previewId }, confirm)
    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ backupId: "backup-20261014-abcd1234", personalMemory: expect.not.objectContaining({ confirmationToken: expect.anything() }) }))
    expect(invoke).toHaveBeenLastCalledWith("restore", {
      restoreBackupPath: snapshots.snapshot.path,
      activate: true,
      personalMemoryConfirmation: "restore-personal-memory:backup-20261014-abcd1234"
    })
    expect(activated).toMatchObject({ ok: true, state: "ACTIVATED", backupId: "backup-20261014-abcd1234" })
    await expect(service.activate({ previewId: preview.previewId }, confirm)).resolves.toMatchObject({ ok: false, code: "runtime_restore_preview_unavailable" })
  })

  it("rejects a personal-memory preview without the exact owner confirmation token before stop or drain", async () => {
    const invalid = previewResponse()
    delete (invalid.data.personal_memory_restore as Record<string, unknown>).confirmation_token
    const invoke = vi.fn().mockResolvedValue(invalid)
    const lifecycle = { status: status("ready"), refresh: vi.fn(), stop: vi.fn() }
    const snapshots = snapshotStore()
    const service = new RuntimeMaintenanceService({ invoke } as never, lifecycle, snapshots.store)
    await expect(service.preview("C:\\private\\selected.zip")).resolves.toMatchObject({ ok: false, code: "runtime_restore_preview_invalid" })
    expect(lifecycle.refresh).not.toHaveBeenCalled()
    expect(lifecycle.stop).not.toHaveBeenCalled()
  })

  it("does not stop the runtime when the native activation confirmation is cancelled", async () => {
    const invoke = vi.fn().mockResolvedValue(previewResponse(false))
    const lifecycle = { status: status("ready"), refresh: vi.fn(), stop: vi.fn() }
    const snapshots = snapshotStore()
    const service = new RuntimeMaintenanceService({ invoke } as never, lifecycle, snapshots.store)
    const preview = await service.preview("C:\\private\\selected.zip")
    await expect(service.activate({ previewId: preview.previewId }, vi.fn().mockResolvedValue(false))).resolves.toMatchObject({ cancelled: true, state: "CANCELLED" })
    expect(lifecycle.refresh).not.toHaveBeenCalled()
    expect(lifecycle.stop).not.toHaveBeenCalled()
  })

  it("rejects a private archive snapshot changed after preview before native confirmation or stop", async () => {
    const invoke = vi.fn().mockResolvedValue(previewResponse(false))
    const lifecycle = { status: status("ready"), refresh: vi.fn(), stop: vi.fn() }
    const snapshots = snapshotStore()
    snapshots.store.verify.mockResolvedValueOnce(true).mockResolvedValueOnce(false)
    const service = new RuntimeMaintenanceService({ invoke } as never, lifecycle, snapshots.store)
    const preview = await service.preview("C:\\shared\\selected.zip")
    const confirm = vi.fn()
    await expect(service.activate({ previewId: preview.previewId }, confirm)).resolves.toMatchObject({ ok: false, code: "runtime_restore_archive_changed" })
    expect(confirm).not.toHaveBeenCalled()
    expect(lifecycle.refresh).not.toHaveBeenCalled()
    expect(lifecycle.stop).not.toHaveBeenCalled()
    expect(invoke).toHaveBeenCalledTimes(1)
  })

  it("expires preview receipts and never substitutes renderer-provided paths or tokens", async () => {
    let now = 1_000
    const invoke = vi.fn().mockResolvedValue(previewResponse(false))
    const lifecycle = { status: status("stopped"), refresh: vi.fn(), stop: vi.fn() }
    const snapshots = snapshotStore()
    const service = new RuntimeMaintenanceService({ invoke } as never, lifecycle, snapshots.store, () => now, 500)
    const preview = await service.preview("C:\\private\\selected.zip")
    now = 2_000
    await expect(service.activate({ previewId: preview.previewId, backupPath: "C:\\evil.zip", confirmationToken: "evil" }, vi.fn())).resolves.toMatchObject({ ok: false, code: "runtime_restore_activation_invalid" })
    await expect(service.activate({ previewId: preview.previewId }, vi.fn())).resolves.toMatchObject({ ok: false, code: "runtime_restore_preview_unavailable" })
    expect(lifecycle.stop).not.toHaveBeenCalled()
  })

  it("stops only an owned active instance before creating a backup", async () => {
    const invoke = vi.fn().mockResolvedValue({ ok: true, command: "backup", state: "COMPLETE", code: "desktop_backup_created", message: "created", data: { backup_id: "backup-1", file_count: 4, total_bytes: 1024 } })
    const lifecycle = { status: status("ready"), refresh: vi.fn().mockResolvedValue(status("ready")), stop: vi.fn().mockResolvedValue(status("stopped")) }
    const snapshots = snapshotStore()
    const service = new RuntimeMaintenanceService({ invoke } as never, lifecycle, snapshots.store)
    await expect(service.backup("C:\\safe\\backup.zip")).resolves.toMatchObject({ ok: true, backup: { fileName: "backup.zip", fileCount: 4, totalBytes: 1024 } })
    expect(lifecycle.stop).toHaveBeenCalledOnce()
    expect(invoke).toHaveBeenCalledWith("backup", { backupDestination: "C:\\safe\\backup.zip" })
  })
})

