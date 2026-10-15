import { randomBytes, randomUUID } from "node:crypto"
import path from "node:path"
import type {
  DataClearApplyResult,
  DataClearPreviewResult,
  DataClearScope,
  DataClearStatusResult,
  RuntimeCliResponse
} from "./contracts"
import { DATA_CLEAR_CONFIRMATION_TEXT } from "./contracts"
import { RuntimeClient, RuntimeInvocationError } from "./runtime-client"

const MAX_PREVIEWS = 8
const BLOCKING_STATES = new Set(["PREPARED", "FINALIZING", "FAILED_PARTIAL"])
const HEX_OPERATION_ID = /^[0-9a-f]{32}$/
const DECIMAL_CREATION_TIME = /^[1-9][0-9]{0,31}$/

interface PreviewReceipt {
  publicId: string
  privateId: string
  expiresAtMs: number
  used: boolean
  preview: DataClearPreviewResult
}

export interface ElectronProcessIdentity {
  pid: number
  creationTime: string
}

export interface OwnedDataClearFinalizerRequest {
  version: 1
  operationId: string
  finalizerCapability: string
  electron: ElectronProcessIdentity
  readyNonce: string
}

export type ConfirmOwnedDataClear = (preview: DataClearPreviewResult) => Promise<boolean>
export type ConfirmFreshDataClearLifecycle = () => Promise<boolean>
export type PrepareOwnedDataClearExit = (request: OwnedDataClearFinalizerRequest) => Promise<boolean>
export type ReadElectronProcessIdentity = () => Promise<ElectronProcessIdentity>

export interface OwnedDataClearController {
  readonly businessAdmissionClosed: boolean
  readonly destructiveExitPending: boolean
  preview(): Promise<DataClearPreviewResult>
  apply(value: unknown, freshLifecycle: ConfirmFreshDataClearLifecycle, confirm: ConfirmOwnedDataClear): Promise<DataClearApplyResult>
  status(): Promise<DataClearStatusResult>
}

export class UnavailableOwnedDataClearController implements OwnedDataClearController {
  readonly businessAdmissionClosed = true
  readonly destructiveExitPending = false
  private result(): DataClearStatusResult {
    return { ok: false, state: "UNKNOWN", code: "desktop_data_clear_bridge_unavailable", message: "本机数据清除 bridge 不可用。" }
  }
  preview(): Promise<DataClearPreviewResult> { return Promise.resolve(this.result()) }
  apply(): Promise<DataClearApplyResult> { return Promise.resolve(this.result()) }
  status(): Promise<DataClearStatusResult> { return Promise.resolve(this.result()) }
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value).sort()
  const expected = [...keys].sort()
  return actual.length === expected.length && actual.every((key, index) => key === expected[index])
}

function boundedString(value: unknown, maximum: number): string | null {
  return typeof value === "string" && value.length > 0 && Buffer.byteLength(value, "utf8") <= maximum && !/[\u0000-\u001f\u007f]/.test(value) ? value : null
}

function boundedStringArray(value: unknown): string[] | null {
  if (!Array.isArray(value) || value.length > 20) return null
  const result: string[] = []
  for (const item of value) {
    const text = boundedString(item, 500)
    if (!text) return null
    result.push(text)
  }
  return result
}

function base(response: RuntimeCliResponse): DataClearStatusResult {
  return { ok: response.ok, state: response.state, code: response.code, message: response.message }
}

function failure(error: unknown, fallback = "desktop_data_clear_failed"): DataClearStatusResult {
  if (error instanceof RuntimeInvocationError) {
    return { ok: false, state: "UNKNOWN", code: error.code, message: "本机数据清除 owner 调用失败；不会报告清除成功。" }
  }
  const code = error && typeof error === "object" && "code" in error ? String(error.code) : fallback
  return { ok: false, state: "UNKNOWN", code, message: "本机数据清除事务失败；不会报告清除成功。" }
}

function publicScope(value: unknown, expectedProfilePath: string): DataClearScope | null {
  const scope = record(value)
  if (!scope) return null
  const profilePath = boundedString(scope.profilePath, 4096)
  const includes = boundedStringArray(scope.includes)
  const preserves = boundedStringArray(scope.preserves)
  const credentialPolicy = boundedString(scope.credentialPolicy, 500)
  if (
    !profilePath || path.resolve(profilePath) !== expectedProfilePath ||
    !Number.isSafeInteger(scope.fileCount) || Number(scope.fileCount) < 0 ||
    !Number.isSafeInteger(scope.totalBytes) || Number(scope.totalBytes) < 0 ||
    !includes || !preserves || !credentialPolicy
  ) return null
  return { profilePath, fileCount: Number(scope.fileCount), totalBytes: Number(scope.totalBytes), includes, preserves, credentialPolicy }
}

function validateElectronIdentity(value: ElectronProcessIdentity, expectedPid: number): ElectronProcessIdentity | null {
  if (!Number.isSafeInteger(value.pid) || value.pid !== expectedPid || value.pid < 1 || value.pid > 2 ** 31 - 1) return null
  if (!DECIMAL_CREATION_TIME.test(value.creationTime)) return null
  return { pid: value.pid, creationTime: value.creationTime }
}

function cancelled(message: string): DataClearApplyResult {
  return { ok: false, state: "CANCELLED", code: "desktop_data_clear_cancelled", message, cancelled: true }
}

export class OwnedDataClearService implements OwnedDataClearController {
  private readonly receipts = new Map<string, PreviewReceipt>()
  private applying = false
  private admissionClosed = false
  private exitPending = false

  constructor(
    private readonly client: RuntimeClient,
    private readonly readElectronIdentity: ReadElectronProcessIdentity,
    private readonly prepareExit: PrepareOwnedDataClearExit,
    private readonly electronPid = process.pid,
    private readonly now: () => number = Date.now
  ) {}

  get businessAdmissionClosed(): boolean { return this.admissionClosed }
  get destructiveExitPending(): boolean { return this.exitPending }

  async preview(): Promise<DataClearPreviewResult> {
    if (this.applying) return { ok: false, state: "BLOCKED", code: "desktop_data_clear_busy", message: "另一个数据清除事务正在核对或收尾。" }
    try {
      const response = await this.client.invoke("data-clear-preview")
      if (!response.ok) return base(response)
      const privateId = boundedString(response.data.previewId, 256)
      const expiresAt = boundedString(response.data.expiresAt, 100)
      const expiresAtMs = expiresAt ? Date.parse(expiresAt) : Number.NaN
      const expectedProfilePath = path.resolve(this.client.layout.dataRoot, "..")
      const scope = publicScope(response.data.scope, expectedProfilePath)
      if (response.state !== "CONFIRMATION_REQUIRED" || !privateId || !expiresAt || !Number.isFinite(expiresAtMs) || expiresAtMs <= this.now() || !scope || response.data.stopped !== true) {
        return { ok: false, state: "UNKNOWN", code: "desktop_data_clear_preview_protocol_invalid", message: "数据清除预览回执无效；未授权删除。" }
      }
      this.prune()
      const publicId = randomUUID()
      const preview: DataClearPreviewResult = {
        ok: true,
        state: response.state,
        code: response.code,
        message: response.message,
        previewId: publicId,
        expiresAt,
        scope,
        stopped: true
      }
      while (this.receipts.size >= MAX_PREVIEWS) this.receipts.delete(this.receipts.keys().next().value as string)
      this.receipts.set(publicId, { publicId, privateId, expiresAtMs, used: false, preview: structuredClone(preview) })
      this.admissionClosed = true
      this.exitPending = false
      return structuredClone(preview)
    } catch (error) {
      return failure(error, "desktop_data_clear_preview_failed")
    }
  }

  async apply(value: unknown, freshLifecycle: ConfirmFreshDataClearLifecycle, confirm: ConfirmOwnedDataClear): Promise<DataClearApplyResult> {
    const request = record(value)
    if (!request || !exactKeys(request, ["previewId", "confirmationText"]) || typeof request.previewId !== "string" || request.confirmationText !== DATA_CLEAR_CONFIRMATION_TEXT) {
      return { ok: false, state: "REJECTED", code: "desktop_data_clear_confirmation_mismatch", message: `必须逐字输入“${DATA_CLEAR_CONFIRMATION_TEXT}”；未更改任何数据。` }
    }
    this.prune()
    const receipt = this.receipts.get(request.previewId)
    if (!receipt || receipt.used || receipt.expiresAtMs <= this.now()) {
      return { ok: false, state: "CONFLICT", code: "desktop_data_clear_preview_invalid", message: "数据清除预览已过期、已使用或不匹配；请重新预览。" }
    }
    if (this.applying) return { ok: false, state: "BLOCKED", code: "desktop_data_clear_busy", message: "另一个数据清除事务正在核对或收尾。" }
    this.applying = true
    try {
      if (!await freshLifecycle()) return cancelled("页面拒绝中断当前草稿或任务；未更改任何数据。")
      if (!await confirm(structuredClone(receipt.preview))) return cancelled("已取消本机数据清除；未更改任何数据。")
      if (!await freshLifecycle()) return cancelled("确认期间页面工作状态发生变化；未更改任何数据。")
      if (receipt.used || receipt.expiresAtMs <= this.now() || this.receipts.get(receipt.publicId) !== receipt) {
        return { ok: false, state: "CONFLICT", code: "desktop_data_clear_preview_invalid", message: "确认期间预览已失效；请重新预览。" }
      }
      const electron = validateElectronIdentity(await this.readElectronIdentity(), this.electronPid)
      if (!electron) return { ok: false, state: "BLOCKED", code: "desktop_data_clear_electron_identity_invalid", message: "无法取得当前 Electron 进程的精确 OS 创建身份；未准备清除。" }
      receipt.used = true
      const response = await this.client.invokePrivate("data-clear-apply", {
        version: 1,
        previewId: receipt.privateId,
        confirmationText: DATA_CLEAR_CONFIRMATION_TEXT,
        electron
      })
      if (!response.ok) return base(response)
      const operationId = boundedString(response.data.operationId, 64)
      const finalizerCapability = boundedString(response.data.finalizerCapability, 256)
      if (response.state !== "PREPARED" || !operationId || !HEX_OPERATION_ID.test(operationId) || response.data.exitRequired !== true || !finalizerCapability) {
        return { ok: false, state: "UNKNOWN", code: "desktop_data_clear_apply_protocol_invalid", message: "owner 未返回可信的 PREPARED 回执；不会报告数据已清除。" }
      }
      this.admissionClosed = true
      this.exitPending = true
      const finalizerRequest: OwnedDataClearFinalizerRequest = {
        version: 1,
        operationId,
        finalizerCapability,
        electron,
        readyNonce: randomBytes(24).toString("base64url")
      }
      if (!await this.prepareExit(finalizerRequest)) {
        return { ok: false, state: "PREPARED", code: "desktop_data_clear_host_exit_not_ready", message: "清除已进入 PREPARED，但宿主会话或 finalizer 未就绪；启动保持阻塞，数据尚未报告为已清除。", operationId, exitRequired: true }
      }
      return { ok: true, state: "PREPARED", code: response.code, message: response.message, operationId, exitRequired: true }
    } catch (error) {
      return failure(error, "desktop_data_clear_apply_failed")
    } finally {
      this.applying = false
    }
  }

  async status(): Promise<DataClearStatusResult> {
    try {
      const response = await this.client.invoke("data-clear-status")
      const result = base(response)
      const operationId = boundedString(response.data.operationId, 64)
      if (operationId && HEX_OPERATION_ID.test(operationId)) result.operationId = operationId
      if (response.state === "CLEARED") {
        const completedAt = boundedString(response.data.completedAt, 100)
        if (!response.ok || !operationId || !HEX_OPERATION_ID.test(operationId) || !completedAt || !Number.isFinite(Date.parse(completedAt))) {
          this.admissionClosed = true
          return { ok: false, state: "UNKNOWN", code: "desktop_data_clear_status_protocol_invalid", message: "CLEARED 回执缺少 finalizer 完成证明；启动保持阻塞。" }
        }
        result.completedAt = completedAt
        this.admissionClosed = false
        this.exitPending = false
      } else if (BLOCKING_STATES.has(response.state) || !response.ok || response.state === "UNKNOWN") {
        this.admissionClosed = true
        if (response.state === "PREPARED" || response.state === "FINALIZING") this.exitPending = true
      }
      return result
    } catch (error) {
      this.admissionClosed = true
      return failure(error, "desktop_data_clear_status_failed")
    }
  }

  private prune(): void {
    const now = this.now()
    for (const [id, receipt] of this.receipts) if (receipt.used || receipt.expiresAtMs <= now) this.receipts.delete(id)
  }
}






