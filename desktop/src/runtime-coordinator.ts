import type { RuntimeCliResponse, RuntimeDiagnosticEvent, RuntimeDiagnosticsView, RuntimePhase, RuntimeStatusView } from "./contracts"
import { RuntimeClient, RuntimeInvocationError } from "./runtime-client"
import { redactText } from "./redaction"

const READY_STATES = new Set(["READY", "RUNNING", "ACTIVE"])
const STOPPED_STATES = new Set(["STOPPED", "NOT_RUNNING", "INACTIVE"])
const STARTING_STATES = new Set(["STARTING", "BOOTING"])
const DEGRADED_STATES = new Set(["DEGRADED", "DRAINING"])
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
    this.unavailable = errorView(error)
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

  constructor(private readonly client: RuntimeClient, private readonly hostVersion: string, private readonly packaged: boolean) {}

  get status(): RuntimeStatusView { return { ...this.current } }
  get diagnostics(): RuntimeDiagnosticsView {
    return { hostVersion: this.hostVersion, packaged: this.packaged, runtime: this.status, recentEvents: this.events.map(event => ({ ...event })) }
  }

  initialize(): Promise<RuntimeStatusView> { return this.startIfStopped("runtime_start_checking", "正在核对 owner runtime 状态。") }
  retry(): Promise<RuntimeStatusView> { return this.startIfStopped("runtime_retry_checking", "正在重新核对 owner runtime 状态。") }
  refresh(): Promise<RuntimeStatusView> {
    if (this.operation) return Promise.resolve(this.status)
    return this.serialized(() => this.run("status"))
  }
  stop(): Promise<RuntimeStatusView> {
    const pending = this.operation
    if (pending) return pending.catch(() => this.status).then(() => this.stop())
    return this.serialized(() => this.run("stop"))
  }

  private startIfStopped(checkCode: string, checkMessage: string): Promise<RuntimeStatusView> {
    return this.serialized(async () => {
      this.markStarting(checkCode, checkMessage)
      const status = await this.run("status")
      if (status.phase === "ready" || status.phase === "starting" || status.phase === "degraded") return status
      if (status.phase !== "stopped") return status
      this.markStarting("runtime_start_verifying", "正在验证受控 runtime release 并等待本机服务健康就绪；完成前不会显示为 READY。")
      const started = await this.run("start")
      // A DEGRADED start response proves launch completion, not the persisted owner reason.
      // Re-read status so local entry depends on the verified health and exact owner reason.
      return started.phase === "degraded" ? await this.run("status") : started
    })
  }

  private markStarting(code: string, message: string): void {
    this.current = {
      phase: "starting",
      state: "STARTING",
      code,
      message,
      canonicalOrigin: null,
      instanceKey: this.current.instanceKey,
      localTransportReady: false,
      canRetry: false,
      checkedAt: new Date().toISOString()
    }
    this.events.push({ at: this.current.checkedAt, command: "host", state: this.current.state, code, message })
    if (this.events.length > 25) this.events.splice(0, this.events.length - 25)
  }

  private serialized(action: () => Promise<RuntimeStatusView>): Promise<RuntimeStatusView> {
    if (this.operation) return this.operation
    const operation = action().finally(() => { if (this.operation === operation) this.operation = null })
    this.operation = operation
    return operation
  }

  private async run(command: "start" | "status" | "stop"): Promise<RuntimeStatusView> {
    try { this.current = viewFor(await this.client.invoke(command)) }
    catch (error) { this.current = errorView(error) }
    this.events.push({ at: this.current.checkedAt, command, state: this.current.state, code: this.current.code, message: this.current.message })
    if (this.events.length > 25) this.events.splice(0, this.events.length - 25)
    return this.status
  }
}

export const runtimeProtocol = {
  phaseFor, trustedCanonicalOrigin, safeInstanceKey, hasTrustedRuntimeIdentity,
  hasVerifiedLocalTransportHealth, hasVerifiedDegradedReason, localTransportReady, viewFor, errorView
}


