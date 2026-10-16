import type { RuntimeCliResponse, RuntimeDiagnosticEvent, RuntimeDiagnosticsView, RuntimePhase, RuntimeStatusView } from "./contracts"
import { RuntimeInvocationError, type RuntimeClient } from "./runtime-client"
import { redactText } from "./redaction"

const READY_STATES = new Set(["READY", "RUNNING", "ACTIVE"])
const STOPPED_STATES = new Set(["STOPPED", "NOT_RUNNING", "INACTIVE"])
const STARTING_STATES = new Set(["STARTING", "BOOTING"])
const DEGRADED_STATES = new Set(["DEGRADED", "DRAINING"])
const STOPPING_STATES = new Set(["STOPPING"])
const PENDING_STATUS_STOP_TIMEOUT_MS = 30_000
const START_CANCELLATION_TIMEOUT_MS = 180_000
const LOCAL_TRANSPORT_HEALTH_KEYS = ["api", "frontend", "bff", "raw_rewrite", "runtime"] as const

export interface RuntimeController {
  readonly status: RuntimeStatusView
  readonly diagnostics: RuntimeDiagnosticsView
  initialize(): Promise<RuntimeStatusView>
  retry(): Promise<RuntimeStatusView>
  refresh(): Promise<RuntimeStatusView>
  stop(): Promise<RuntimeStatusView>
}

function phaseFor(response: RuntimeCliResponse): RuntimePhase {
  const state = response.state.toUpperCase()
  if (!response.ok) return state === "UNKNOWN" ? "unknown" : "error"
  if (READY_STATES.has(state)) return "ready"
  if (STOPPED_STATES.has(state)) return "stopped"
  if (STARTING_STATES.has(state)) return "starting"
  if (STOPPING_STATES.has(state)) return "starting"
  if (DEGRADED_STATES.has(state)) return "degraded"
  return "unknown"
}

function trustedCanonicalOrigin(value: unknown): string | null {
  if (typeof value !== "string") return null
  try {
    const url = new URL(value)
    if (url.protocol !== "http:" || url.username || url.password || url.pathname !== "/" || url.search || url.hash) return null
    if (url.hostname !== "127.0.0.1" || url.port !== "3120") return null
    return url.origin
  } catch {
    return null
  }
}

function safeInstanceKey(value: unknown): string | null {
  if (typeof value !== "string" || value.length < 1 || value.length > 200 || /[\r\n\0]/.test(value)) return null
  return redactText(value, 200)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value)
}

function hasTrustedRuntimeIdentity(canonicalOrigin: string | null, instanceKey: string | null): boolean {
  return canonicalOrigin !== null && instanceKey === `local-desktop:${canonicalOrigin}`
}

function hasVerifiedLocalTransportHealth(value: unknown): boolean {
  return isRecord(value) && LOCAL_TRANSPORT_HEALTH_KEYS.every(key => value[key] === true)
}

function hasVerifiedDegradedReason(response: RuntimeCliResponse): boolean {
  return response.command === "status" && response.code === "desktop_degraded" &&
    response.message === "local desktop health was checked" && response.data.reason_code === "desktop_degraded"
}

function localTransportReady(response: RuntimeCliResponse, phase: RuntimePhase, identityTrusted: boolean): boolean {
  if (!identityTrusted) return false
  if (phase === "ready") return true
  return phase === "degraded" && response.state === "DEGRADED" && hasVerifiedDegradedReason(response) &&
    hasVerifiedLocalTransportHealth(response.data.health)
}

function viewFor(response: RuntimeCliResponse): RuntimeStatusView {
  const phase = phaseFor(response)
  const canonicalOrigin = trustedCanonicalOrigin(response.data.canonical_origin)
  const instanceKey = safeInstanceKey(response.data.instance_key)
  const requiresTrustedIdentity = phase === "ready" || phase === "degraded"
  const identityTrusted = hasTrustedRuntimeIdentity(canonicalOrigin, instanceKey)
  const invalidOrigin = requiresTrustedIdentity && canonicalOrigin === null
  const invalidIdentity = requiresTrustedIdentity && !invalidOrigin && !identityTrusted
  const protocolInvalid = invalidOrigin || invalidIdentity
  return {
    phase: protocolInvalid ? "error" : phase,
    state: invalidOrigin ? "INVALID_ORIGIN" : invalidIdentity ? "INVALID_IDENTITY" : redactText(response.state, 80),
    code: invalidOrigin ? "runtime_origin_invalid" : invalidIdentity ? "runtime_identity_invalid" : redactText(response.code, 120),
    message: invalidOrigin
      ? "Runtime reported an untrusted canonical origin"
      : invalidIdentity ? "Runtime reported an untrusted local instance identity" : redactText(response.message, 500),
    canonicalOrigin: protocolInvalid ? null : canonicalOrigin,
    instanceKey: protocolInvalid ? null : instanceKey,
    localTransportReady: !protocolInvalid && localTransportReady(response, phase, identityTrusted),
    canRetry: protocolInvalid || phase === "error" || phase === "unknown" || phase === "stopped" || phase === "degraded",
    checkedAt: new Date().toISOString()
  }
}

function errorView(error: unknown): RuntimeStatusView {
  const invocation = error instanceof RuntimeInvocationError ? error : null
  const code = error && typeof error === "object" && "code" in error ? String(error.code) : invocation?.code
  return {
    phase: "unknown",
    state: "UNKNOWN",
    code: redactText(code ?? "runtime_unexpected_error", 120),
    message: redactText(invocation?.message ?? (error instanceof Error ? error.message : "Runtime status is unavailable"), 500),
    canonicalOrigin: null,
    instanceKey: null,
    localTransportReady: false,
    canRetry: true,
    checkedAt: new Date().toISOString()
  }
}

export class UnavailableRuntimeController implements RuntimeController {
  private readonly unavailable: RuntimeStatusView

  constructor(error: unknown, private readonly hostVersion: string, private readonly packaged: boolean) {
    // This controller cannot reconstruct a missing runtime client. Do not expose
    // a retry action that can only return the same status without doing work.
    this.unavailable = { ...errorView(error), canRetry: false }
  }

  get status(): RuntimeStatusView { return { ...this.unavailable, checkedAt: new Date().toISOString() } }
  get diagnostics(): RuntimeDiagnosticsView {
    return {
      hostVersion: this.hostVersion,
      packaged: this.packaged,
      runtime: this.status,
      recentEvents: [{ at: this.unavailable.checkedAt, command: "host", state: this.unavailable.state, code: this.unavailable.code, message: this.unavailable.message }]
    }
  }
  initialize(): Promise<RuntimeStatusView> { return Promise.resolve(this.status) }
  retry(): Promise<RuntimeStatusView> { return Promise.resolve(this.status) }
  refresh(): Promise<RuntimeStatusView> { return Promise.resolve(this.status) }
  stop(): Promise<RuntimeStatusView> { return Promise.resolve(this.status) }
}

export interface RuntimeCoordinatorTiming {
  pendingStatusStopTimeoutMs?: number
  startCancellationTimeoutMs?: number
}

type RuntimeLifecycleCommand = "start" | "status" | "stop"
type RuntimeLifecycleClient = Pick<RuntimeClient, "invoke" | "cancelStart">

export class RuntimeCoordinator implements RuntimeController {
  private current: RuntimeStatusView = {
    phase: "unknown",
    state: "UNKNOWN",
    code: "runtime_not_checked",
    message: "Runtime has not been checked yet",
    canonicalOrigin: null,
    instanceKey: null,
    localTransportReady: false,
    canRetry: true,
    checkedAt: new Date(0).toISOString()
  }
  private readonly events: RuntimeDiagnosticEvent[] = []
  private operation: Promise<RuntimeStatusView> | null = null
  private stopOperation: Promise<RuntimeStatusView> | null = null
  private activeCommand: RuntimeLifecycleCommand | null = null
  private stopFence = false

  constructor(
    private readonly client: RuntimeLifecycleClient,
    private readonly hostVersion: string,
    private readonly packaged: boolean,
    private readonly timing: RuntimeCoordinatorTiming = {}
  ) {}

  get status(): RuntimeStatusView { return { ...this.current } }
  get diagnostics(): RuntimeDiagnosticsView {
    return { hostVersion: this.hostVersion, packaged: this.packaged, runtime: this.status, recentEvents: this.events.map(event => ({ ...event })) }
  }

  initialize(): Promise<RuntimeStatusView> { return this.startIfStopped("runtime_start_checking", "正在核对 owner runtime 状态。") }
  retry(): Promise<RuntimeStatusView> { return this.startIfStopped("runtime_retry_checking", "正在重新核对 owner runtime 状态。") }
  refresh(): Promise<RuntimeStatusView> {
    if (this.stopFence || this.operation || this.stopOperation) return Promise.resolve(this.status)
    return this.serialized(() => this.run("status"))
  }
  stop(): Promise<RuntimeStatusView> {
    if (this.stopOperation) return this.stopOperation
    this.stopFence = true
    const stopping = this.stopAfterPending().finally(() => {
      if (this.stopOperation === stopping) this.stopOperation = null
    })
    this.stopOperation = stopping
    return stopping
  }

  private startIfStopped(checkCode: string, checkMessage: string): Promise<RuntimeStatusView> {
    if (this.stopFence || this.stopOperation) return Promise.resolve(this.status)
    return this.serialized(async () => {
      if (this.stopFence) return this.status
      this.markHostState("starting", "STARTING", checkCode, checkMessage, false)
      const status = await this.run("status")
      if (this.stopFence) return status
      if (status.phase === "ready" || status.phase === "starting" || status.phase === "degraded") return status
      if (status.phase !== "stopped") return status
      this.markHostState(
        "starting",
        "STARTING",
        "runtime_start_verifying",
        "正在验证受控 runtime release 并等待本机服务健康就绪；完成前不会显示为 READY。",
        false
      )
      if (this.stopFence) return this.status
      const started = await this.run("start")
      if (this.stopFence) return started
      // A DEGRADED start response proves launch completion, not the persisted owner reason.
      // Re-read status so local entry depends on the verified health and exact owner reason.
      return started.phase === "degraded" ? await this.run("status") : started
    })
  }

  private async stopAfterPending(): Promise<RuntimeStatusView> {
    const pending = this.operation
    const pendingCommand = this.activeCommand
    if (pending) {
      if (pendingCommand === "start") {
        this.markHostState(
          "starting",
          "STOPPING",
          "runtime_start_cancelling",
          "正在请求启动流程在安全检查点停止；确认启动命令退出后仍会执行 owner stop 核验。",
          false
        )
        try {
          await this.client.cancelStart()
        } catch (error) {
          return this.markUnknown(
            error instanceof RuntimeInvocationError ? error.code : "runtime_start_cancel_write_failed",
            "无法确认启动取消请求已送达；窗口将保持打开，且不会强杀身份不明的进程。"
          )
        }
        if (!(await this.awaitOperation(pending, this.timing.startCancellationTimeoutMs ?? START_CANCELLATION_TIMEOUT_MS))) {
          return this.markUnknown(
            "runtime_start_cancel_unconfirmed",
            "启动流程未在有界取消期限内退出；窗口将保持打开，且不会并发执行可能竞态的 stop。"
          )
        }
      } else if (!(await this.awaitOperation(pending, this.timing.pendingStatusStopTimeoutMs ?? PENDING_STATUS_STOP_TIMEOUT_MS))) {
        return this.markUnknown(
          "runtime_pending_status_unconfirmed",
          "状态核对未在有界期限内结束；已阻止后续启动，窗口将保持打开。"
        )
      }
    }

    this.markHostState(
      "starting",
      "STOPPING",
      "runtime_stop_verifying",
      "正在等待 owner runtime 完成 drain、进程收敛并返回可验证的 STOPPED 状态。",
      false
    )
    return await this.run("stop")
  }

  private awaitOperation(operation: Promise<RuntimeStatusView>, timeoutMs: number): Promise<boolean> {
    return new Promise(resolve => {
      let settled = false
      const finish = (value: boolean) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        resolve(value)
      }
      const timer = setTimeout(() => finish(false), timeoutMs)
      void operation.then(() => finish(true), () => finish(true))
    })
  }

  private markUnknown(code: string, message: string): RuntimeStatusView {
    this.current = {
      phase: "unknown",
      state: "UNKNOWN",
      code: redactText(code, 120),
      message: redactText(message, 500),
      canonicalOrigin: null,
      instanceKey: this.current.instanceKey,
      localTransportReady: false,
      canRetry: false,
      checkedAt: new Date().toISOString()
    }
    this.record("host")
    return this.status
  }

  private markHostState(phase: RuntimePhase, state: string, code: string, message: string, canRetry: boolean): void {
    this.current = {
      phase,
      state,
      code,
      message,
      canonicalOrigin: null,
      instanceKey: this.current.instanceKey,
      localTransportReady: false,
      canRetry,
      checkedAt: new Date().toISOString()
    }
    this.record("host")
  }

  private serialized(action: () => Promise<RuntimeStatusView>): Promise<RuntimeStatusView> {
    if (this.operation) return this.operation
    const operation = action().finally(() => { if (this.operation === operation) this.operation = null })
    this.operation = operation
    return operation
  }

  private async run(command: RuntimeLifecycleCommand): Promise<RuntimeStatusView> {
    this.activeCommand = command
    try {
      let observed: RuntimeStatusView
      try { observed = viewFor(await this.client.invoke(command)) }
      catch (error) { observed = errorView(error) }
      const preserveUnconfirmedStop = command !== "stop" && this.stopFence && this.current.phase === "unknown" && (
        this.current.code.startsWith("runtime_start_cancel_") || this.current.code === "runtime_pending_status_unconfirmed"
      )
      if (preserveUnconfirmedStop) {
        this.events.push({ at: observed.checkedAt, command, state: observed.state, code: observed.code, message: observed.message })
        if (this.events.length > 25) this.events.splice(0, this.events.length - 25)
        return this.status
      }
      this.current = this.stopFence && observed.phase !== "stopped"
        ? { ...observed, canRetry: false }
        : observed
      this.record(command)
      return this.status
    } finally {
      if (this.activeCommand === command) this.activeCommand = null
    }
  }

  private record(command: RuntimeLifecycleCommand | "host"): void {
    this.events.push({ at: this.current.checkedAt, command, state: this.current.state, code: this.current.code, message: this.current.message })
    if (this.events.length > 25) this.events.splice(0, this.events.length - 25)
  }
}

export const runtimeProtocol = {
  phaseFor, trustedCanonicalOrigin, safeInstanceKey, hasTrustedRuntimeIdentity,
  hasVerifiedLocalTransportHealth, hasVerifiedDegradedReason, localTransportReady, viewFor, errorView
}
