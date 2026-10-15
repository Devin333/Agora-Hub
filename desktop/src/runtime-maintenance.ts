import { randomUUID } from "node:crypto"
import path from "node:path"
import type {
  PersonalMemoryRestoreSummary,
  RestoreActivateRequest,
  RestoreActivateResult,
  RestorePreviewResult,
  RuntimeBackupResult,
  RuntimeMaintenanceResult,
  RuntimeCliResponse,
  RuntimeStatusView
} from "./contracts"
import type { RuntimeClient } from "./runtime-client"
import type { RestoreArchiveSnapshot, RestoreArchiveSnapshots } from "./restore-archive-snapshot"

const DEFAULT_PREVIEW_TTL_MS = 15 * 60 * 1000
const MAX_RECEIPTS = 8

interface LifecycleController {
  readonly status: RuntimeStatusView
  refresh(): Promise<RuntimeStatusView>
  stop(): Promise<RuntimeStatusView>
}

interface PreviewReceipt {
  previewId: string
  snapshot: RestoreArchiveSnapshot
  backupId: string
  confirmationToken: string | null
  personalMemory?: PersonalMemoryRestoreSummary
  expiresAtMs: number
  used: boolean
}

export interface RestoreNativeConfirmation {
  backupId: string
  personalMemory?: PersonalMemoryRestoreSummary
}

export type ConfirmRestore = (summary: RestoreNativeConfirmation) => Promise<boolean>

export interface RuntimeMaintenanceController {
  backup(destination: string): Promise<RuntimeBackupResult>
  preview(backupPath: string): Promise<RestorePreviewResult>
  activate(value: unknown, confirm: ConfirmRestore): Promise<RestoreActivateResult>
}

function safeCode(value: unknown, fallback: string): string {
  return typeof value === "string" && /^[a-z0-9_.-]{1,120}$/i.test(value) ? value : fallback
}

function safeIdentifier(value: unknown): string | null {
  return typeof value === "string" && /^[A-Za-z0-9_.:-]{1,160}$/.test(value) ? value : null
}

function safeCount(value: unknown): number | null {
  return Number.isSafeInteger(value) && Number(value) >= 0 ? Number(value) : null
}

function maintenanceFailure(code: string, message: string): RestoreActivateResult {
  return { ok: false, code, message, state: "ERROR" }
}

function projectPersonalMemory(value: unknown, backupId: string): { summary: PersonalMemoryRestoreSummary; token: string } | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null
  const record = value as Record<string, unknown>
  const token = typeof record.confirmation_token === "string" ? record.confirmation_token : ""
  if (record.backup_id !== backupId || token !== `restore-personal-memory:${backupId}` || record.requires_explicit_activation_confirmation !== true) return null
  const statusCountsValue = record.status_counts
  if (!statusCountsValue || typeof statusCountsValue !== "object" || Array.isArray(statusCountsValue)) return null
  const entries = Object.entries(statusCountsValue)
  if (entries.length > 24) return null
  const statusCounts: Record<string, number> = {}
  for (const [status, count] of entries) {
    if (!/^[A-Za-z0-9_.-]{1,64}$/.test(status) || safeCount(count) === null) return null
    statusCounts[status] = Number(count)
  }
  const policyCount = safeCount(record.policy_count)
  if (policyCount === null) return null
  const warning = typeof record.warning === "string" ? record.warning.slice(0, 400) : "Restoring this snapshot may bring back old personal memory state."
  return {
    token,
    summary: {
      backupId,
      statusCounts,
      policyCount,
      requiresExplicitActivationConfirmation: true,
      warning
    }
  }
}

function isolationVerified(response: RuntimeCliResponse): boolean {
  const validation = response.data.validation
  if (!validation || typeof validation !== "object" || Array.isArray(validation)) return false
  const isolation = (validation as Record<string, unknown>).application_isolation
  return Boolean(isolation && typeof isolation === "object" && !Array.isArray(isolation) && (isolation as Record<string, unknown>).verified === true)
}

export class RuntimeMaintenanceService implements RuntimeMaintenanceController {
  private readonly receipts = new Map<string, PreviewReceipt>()

  constructor(
    private readonly client: RuntimeClient,
    private readonly lifecycle: LifecycleController,
    private readonly snapshots: RestoreArchiveSnapshots,
    private readonly now: () => number = Date.now,
    private readonly previewTtlMs = DEFAULT_PREVIEW_TTL_MS
  ) {}

  async backup(destination: string): Promise<RuntimeBackupResult> {
    const stopped = await this.ensureStopped()
    if (!stopped.ok) return stopped
    try {
      const response = await this.client.invoke("backup", { backupDestination: destination })
      if (!response.ok || response.state !== "COMPLETE") return { ok: false, state: response.state, code: safeCode(response.code, "runtime_backup_failed"), message: "本机备份未完成。" }
      return {
        ok: true,
        state: response.state,
        code: safeCode(response.code, "desktop_backup_created"),
        message: "本机备份已创建。",
        backup: {
          backupId: safeIdentifier(response.data.backup_id),
          fileName: path.basename(destination),
          fileCount: safeCount(response.data.file_count),
          totalBytes: safeCount(response.data.total_bytes)
        }
      }
    } catch (error) {
      return { ok: false, state: "ERROR", code: this.errorCode(error, "runtime_backup_failed"), message: "本机备份调用失败。" }
    }
  }

  async preview(backupPath: string): Promise<RestorePreviewResult> {
    await this.pruneReceipts()
    let snapshot: RestoreArchiveSnapshot | null = null
    try {
      snapshot = await this.snapshots.create(backupPath)
      const response = await this.client.invoke("restore", { restoreBackupPath: snapshot.path })
      if (!response.ok || response.state !== "VERIFIED") {
        await this.snapshots.remove(snapshot)
        return { ok: false, state: response.state, code: safeCode(response.code, "runtime_restore_preview_failed"), message: "备份未通过隔离恢复预览。" }
      }
      if (!(await this.snapshots.verify(snapshot))) {
        await this.snapshots.remove(snapshot)
        return { ok: false, state: "ERROR", code: "runtime_restore_archive_changed", message: "受控恢复归档在预览期间发生变化，已拒绝继续。" }
      }
      const backupId = safeIdentifier(response.data.backup_id)
      if (!backupId || !isolationVerified(response)) {
        await this.snapshots.remove(snapshot)
        return { ok: false, state: "ERROR", code: "runtime_restore_preview_invalid", message: "恢复预览缺少有效的隔离验证回执。" }
      }

      let confirmationToken: string | null = null
      let personalMemory: PersonalMemoryRestoreSummary | undefined
      if (response.data.personal_memory_restore !== null && response.data.personal_memory_restore !== undefined) {
        const projected = projectPersonalMemory(response.data.personal_memory_restore, backupId)
        if (!projected) {
          await this.snapshots.remove(snapshot)
          return { ok: false, state: "ERROR", code: "runtime_restore_preview_invalid", message: "个人记忆恢复回执无效。" }
        }
        confirmationToken = projected.token
        personalMemory = projected.summary
      }

      const previewId = randomUUID()
      const expiresAtMs = this.now() + this.previewTtlMs
      this.receipts.set(previewId, { previewId, snapshot, backupId, confirmationToken, personalMemory, expiresAtMs, used: false })
      await this.trimReceipts()
      return {
        ok: true,
        state: response.state,
        code: safeCode(response.code, "desktop_restore_verified"),
        message: "备份已从受控快照在隔离目录中验证，尚未激活。",
        previewId,
        backupId,
        isolationVerified: true,
        ...(personalMemory ? { personalMemory } : {}),
        expiresAt: new Date(expiresAtMs).toISOString()
      }
    } catch (error) {
      if (snapshot) await this.snapshots.remove(snapshot)
      return { ok: false, state: "ERROR", code: this.errorCode(error, "runtime_restore_preview_failed"), message: "隔离恢复预览调用失败。" }
    }
  }

  async activate(value: unknown, confirm: ConfirmRestore): Promise<RestoreActivateResult> {
    await this.pruneReceipts()
    const request = this.validateActivateRequest(value)
    if (!request) return maintenanceFailure("runtime_restore_activation_invalid", "恢复激活请求无效。")
    const receipt = this.receipts.get(request.previewId)
    if (!receipt || receipt.used) return maintenanceFailure("runtime_restore_preview_unavailable", "恢复预览已过期、已使用或不存在。")
    if (receipt.personalMemory && !receipt.confirmationToken) return maintenanceFailure("runtime_restore_confirmation_missing", "个人记忆恢复确认回执缺失。")
    if (!(await this.snapshots.verify(receipt.snapshot))) {
      receipt.used = true
      await this.snapshots.remove(receipt.snapshot)
      return maintenanceFailure("runtime_restore_archive_changed", "受控恢复归档已变化或损坏，未执行激活。")
    }

    if (!(await confirm({ backupId: receipt.backupId, ...(receipt.personalMemory ? { personalMemory: receipt.personalMemory } : {}) }))) {
      return { ok: false, state: "CANCELLED", code: "cancelled", message: "已取消恢复激活。", cancelled: true }
    }
    if (!(await this.snapshots.verify(receipt.snapshot))) {
      receipt.used = true
      await this.snapshots.remove(receipt.snapshot)
      return maintenanceFailure("runtime_restore_archive_changed", "确认期间受控恢复归档发生变化，未执行激活。")
    }

    receipt.used = true
    const stopped = await this.ensureStopped()
    if (!stopped.ok) {
      await this.snapshots.remove(receipt.snapshot)
      return { ...stopped, backupId: receipt.backupId }
    }
    if (!(await this.snapshots.verify(receipt.snapshot))) {
      await this.snapshots.remove(receipt.snapshot)
      return maintenanceFailure("runtime_restore_archive_changed", "停止实例期间受控恢复归档发生变化，未执行激活。")
    }
    try {
      const response = await this.client.invoke("restore", {
        restoreBackupPath: receipt.snapshot.path,
        activate: true,
        ...(receipt.confirmationToken ? { personalMemoryConfirmation: receipt.confirmationToken } : {})
      })
      if (!response.ok || response.state !== "ACTIVATED" || response.data.backup_id !== receipt.backupId) {
        return { ok: false, state: response.state, code: safeCode(response.code, "runtime_restore_activation_failed"), message: "恢复未被 owner manager 确认为已激活。", backupId: receipt.backupId }
      }
      return { ok: true, state: response.state, code: safeCode(response.code, "desktop_restore_activated"), message: "备份已由 owner manager 激活；旧会话已按恢复策略失效。", backupId: receipt.backupId }
    } catch (error) {
      return { ok: false, state: "ERROR", code: this.errorCode(error, "runtime_restore_activation_failed"), message: "恢复激活调用失败。", backupId: receipt.backupId }
    } finally {
      await this.snapshots.remove(receipt.snapshot)
    }
  }

  private async ensureStopped(): Promise<RuntimeMaintenanceResult> {
    const status = await this.lifecycle.refresh()
    if (status.phase === "stopped") return { ok: true, state: status.state, code: status.code, message: "Runtime is stopped." }
    const ownedActive = Boolean(status.instanceKey) || status.phase === "ready" || status.phase === "starting" || status.phase === "degraded"
    if (!ownedActive) return { ok: false, state: status.state, code: "runtime_stop_unconfirmed", message: "无法确认当前实例归属和停止状态；未执行维护操作。" }
    const stopped = await this.lifecycle.stop()
    if (stopped.phase !== "stopped") return { ok: false, state: stopped.state, code: "runtime_stop_unconfirmed", message: "既有 owner manager 未确认实例已安全停止；未执行维护操作。" }
    return { ok: true, state: stopped.state, code: stopped.code, message: "Runtime is stopped." }
  }

  private validateActivateRequest(value: unknown): RestoreActivateRequest | null {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null
    const record = value as Record<string, unknown>
    if (Object.keys(record).length !== 1 || typeof record.previewId !== "string" || !/^[0-9a-f-]{36}$/i.test(record.previewId)) return null
    return { previewId: record.previewId }
  }

  private async pruneReceipts(): Promise<void> {
    const now = this.now()
    for (const [id, receipt] of this.receipts) {
      if (receipt.used || receipt.expiresAtMs <= now) {
        this.receipts.delete(id)
        await this.snapshots.remove(receipt.snapshot)
      }
    }
  }

  private async trimReceipts(): Promise<void> {
    while (this.receipts.size > MAX_RECEIPTS) {
      const id = this.receipts.keys().next().value as string
      const receipt = this.receipts.get(id)
      this.receipts.delete(id)
      if (receipt) await this.snapshots.remove(receipt.snapshot)
    }
  }

  private errorCode(error: unknown, fallback: string): string {
    return error && typeof error === "object" && "code" in error ? safeCode(String(error.code), fallback) : fallback
  }
}

export class UnavailableRuntimeMaintenanceController implements RuntimeMaintenanceController {
  constructor(private readonly code = "runtime_maintenance_unavailable") {}
  backup(): Promise<RuntimeBackupResult> { return Promise.resolve({ ok: false, state: "ERROR", code: this.code, message: "本机维护运行时不可用。" }) }
  preview(): Promise<RestorePreviewResult> { return Promise.resolve({ ok: false, state: "ERROR", code: this.code, message: "本机维护运行时不可用。" }) }
  activate(): Promise<RestoreActivateResult> { return Promise.resolve({ ok: false, state: "ERROR", code: this.code, message: "本机维护运行时不可用。" }) }
}
