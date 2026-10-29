import type { ConnectionChoice, ConnectionMode, ConnectionPhase, ConnectionSaveResult, ConnectionStatusView, LocalImageRegistryId, LocalStackView } from "./contracts"
import { connectionFailure, describeHttpStatus, describeNetworkError, isSupersededLoad, type ConnectionFailure } from "./load-failure"
import { validateLocalAdmin, type LocalAdminInput } from "./local-stack-admin"
import type { LocalAdminResult, LocalStackResult } from "./local-stack"
import type { ServerConnectionStore } from "./server-connection"
import { parseServerOrigin, serverOriginErrorMessage } from "./server-origin"

/**
 * Drives the one window between the packaged connection page and the
 * workspace of the chosen connection.
 *
 * Every attempt first resolves the origin (for local mode: brings the stack
 * on this computer up), probes it, then loads the workspace; a failure at any
 * step, or a later failure of the loaded workspace, brings the connection page
 * back with a fixed explanation and, for transient causes, an automatic retry
 * with growing delays. Each attempt carries a generation, so a result that
 * arrives after a newer attempt started is ignored.
 *
 * A new choice is verified before it is saved: the server must answer as a
 * hosted Agora service, the local stack must be running (and have its first
 * administrator); only then is the choice written and opened, so a failed
 * choice leaves the previous one in place.
 */
export interface WorkspaceSurface {
  /** Shows the packaged connection page; a no-op while it already shows. */
  showConnectionPage(): Promise<void>
  /** Loads the workspace; rejects with the Chromium error when the main frame failed. */
  showWorkspace(url: string): Promise<void>
  /** The URL of the document the window shows now. */
  currentUrl(): string
}

export interface RetryTimer {
  set(callback: () => void, delayMs: number): unknown
  clear(handle: unknown): void
  now(): number
}

/** The part of the local stack controller the connection drives. */
export interface LocalStackPort {
  readonly view: LocalStackView
  readonly busy: boolean
  ensureRunning(): Promise<LocalStackResult>
  createAdmin(input: LocalAdminInput): Promise<LocalAdminResult>
  setRegistry(registry: LocalImageRegistryId): Promise<void>
  cancel(): void
  stop(): Promise<{ ok: boolean }>
  onChange(listener: (view: LocalStackView) => void): () => void
}

export interface WorkspaceConnectionOptions {
  store: ServerConnectionStore
  local: LocalStackPort
  surface: WorkspaceSurface
  probe(origin: string): Promise<ConnectionFailure | null>
  timer: RetryTimer
  isWorkspaceUrl(url: string, origin: string): boolean
  onEvent?(event: string, details: Record<string, unknown>): void
}

export const RETRY_DELAYS_MS: readonly number[] = [2_000, 5_000, 10_000, 30_000, 60_000]
export const WORKSPACE_PATH = "/paper-agent"

export const CHOICE_MESSAGES = Object.freeze({
  official_unavailable: "这个版本没有配置官方服务。",
  choice_invalid: "请选择一种连接方式。",
  local_cancelled: "已取消启动本机服务。",
  local_admin_required: "本机服务已启动，请先设置管理员账号。",
  not_choosing: "现在不能更换连接方式，请稍后重试。"
})

export function workspaceUrl(origin: string): string {
  return new URL(WORKSPACE_PATH, origin).href
}

type Resolution = { origin: string } | { stop: true }

export class WorkspaceConnection {
  private phase: ConnectionPhase = "unconfigured"
  private failure: ConnectionFailure | null = null
  /** The origin the current attempt resolved; null until it has one. */
  private activeOrigin: string | null = null
  private generation = 0
  private retryHandle: unknown = null
  private nextRetryAt: number | null = null
  private retryAttempt = 0
  /** A failure observed while the workspace was still loading, reported once the load settles. */
  private loadFailure: ConnectionFailure | null = null
  private readonly listeners = new Set<(status: ConnectionStatusView) => void>()
  private readonly removeLocalListener: () => void

  constructor(private readonly options: WorkspaceConnectionOptions) {
    // The page follows the stack's progress while it prepares.
    this.removeLocalListener = options.local.onChange(() => this.notify())
  }

  get status(): ConnectionStatusView {
    const view = this.options.store.view
    return {
      phase: this.phase,
      mode: view.mode,
      origin: this.activeOrigin ?? this.configuredOrigin(view.mode),
      officialOrigin: view.officialOrigin,
      serverOrigin: view.serverOrigin,
      failure: this.failure,
      nextRetryAt: this.nextRetryAt,
      allowLoopbackHttp: this.options.store.policy.allowLoopbackHttp,
      local: this.options.local.view
    }
  }

  /** The origin whose pages may load and use the workspace bridge right now. */
  get workspaceOrigin(): string | null {
    return this.phase === "connecting" || this.phase === "connected" ? this.activeOrigin : null
  }

  get connected(): boolean {
    return this.phase === "connected"
  }

  get mode(): ConnectionMode | null {
    return this.options.store.view.mode
  }

  onChange(listener: (status: ConnectionStatusView) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  /** Starts (or restarts) an attempt against the saved connection. */
  async connect(): Promise<ConnectionStatusView> {
    const generation = this.beginAttempt()
    const mode = this.options.store.view.mode
    if (!mode) {
      this.transition("unconfigured", null)
      await this.options.surface.showConnectionPage()
      return this.status
    }
    this.transition("connecting", null)
    // The page shows the attempt's progress; the surface leaves it alone when it is already showing.
    await this.options.surface.showConnectionPage()
    const resolution = await this.resolve(mode, generation)
    if (generation !== this.generation || "stop" in resolution) return this.status
    await this.open(resolution.origin, generation)
    return this.status
  }

  /** A retry the user asked for, or the connection page's reaction to the network coming back. */
  async retry(): Promise<ConnectionStatusView> {
    // An attempt in flight, a shown workspace or an open form waits for the user.
    if (this.phase === "connected" || this.phase === "connecting" || this.phase === "editing" || this.phase === "local-setup") return this.status
    this.retryAttempt = 0
    return await this.connect()
  }

  /** Verifies a new connection, stores it, then opens it. */
  async choose(value: unknown): Promise<ConnectionSaveResult> {
    if (this.phase !== "unconfigured" && this.phase !== "editing") return { ok: false, code: "not_choosing", message: CHOICE_MESSAGES.not_choosing }
    const choice = parseChoice(value)
    if (!choice) return { ok: false, code: "choice_invalid", message: CHOICE_MESSAGES.choice_invalid }
    const generation = this.generation
    if (choice.mode !== "local" && this.options.local.busy) this.options.local.cancel()
    if (choice.mode === "official") {
      const origin = this.options.store.view.officialOrigin
      if (!origin) return { ok: false, code: "official_unavailable", message: CHOICE_MESSAGES.official_unavailable }
      const failure = await this.options.probe(origin)
      if (failure) return { ok: false, code: failure.code, message: failure.message }
      if (generation !== this.generation) return { ok: false, code: "not_choosing", message: CHOICE_MESSAGES.not_choosing }
      await this.options.store.saveOfficial()
    } else if (choice.mode === "server") {
      const parsed = parseServerOrigin(choice.address, this.options.store.policy)
      if (!parsed.ok) return { ok: false, code: parsed.code, message: serverOriginErrorMessage(parsed.code) }
      // A wrong address is rejected here rather than saved and failed against later.
      const failure = await this.options.probe(parsed.origin)
      if (failure) return { ok: false, code: failure.code, message: failure.message }
      if (generation !== this.generation) return { ok: false, code: "not_choosing", message: CHOICE_MESSAGES.not_choosing }
      await this.options.store.saveServer(parsed.origin)
    } else {
      if (choice.registry) await this.options.local.setRegistry(choice.registry)
      const result = await this.options.local.ensureRunning()
      if (result.status === "cancelled") return { ok: false, code: "local_cancelled", message: CHOICE_MESSAGES.local_cancelled }
      if (result.status === "failed") return { ok: false, code: result.failure.code, message: result.failure.message }
      // The first administrator is created next; the page shows the form from the stack's state.
      if (result.status === "needs-admin") return { ok: false, code: "local_admin_required", message: CHOICE_MESSAGES.local_admin_required }
      if (generation !== this.generation) return { ok: false, code: "not_choosing", message: CHOICE_MESSAGES.not_choosing }
      await this.options.store.saveLocal()
    }
    this.options.onEvent?.("connection_saved", { mode: choice.mode })
    this.retryAttempt = 0
    void this.connect()
    return { ok: true }
  }

  /** Creates the local stack's first administrator, then saves local mode (if it was being chosen) and opens it. */
  async createLocalAdmin(value: unknown): Promise<ConnectionSaveResult> {
    const validation = validateLocalAdmin(value)
    if (!validation.ok) return validation
    const result = await this.options.local.createAdmin(validation.input)
    if (!result.ok) return result
    if (this.options.store.view.mode !== "local") await this.options.store.saveLocal()
    this.options.onEvent?.("connection_saved", { mode: "local" })
    this.retryAttempt = 0
    void this.connect()
    return { ok: true }
  }

  /** Interrupts a local stack start the user no longer wants to wait for. */
  cancelLocal(): ConnectionStatusView {
    this.options.local.cancel()
    return this.status
  }

  /** Leaves the workspace to let the user choose another connection; the caller already settled unsaved work. */
  async edit(): Promise<void> {
    this.beginAttempt()
    // A slow local start (an image download) must not keep running behind the form.
    if (this.options.local.busy) this.options.local.cancel()
    this.transition("editing", null)
    await this.options.surface.showConnectionPage()
  }

  async cancelEdit(): Promise<ConnectionStatusView> {
    if (this.phase !== "editing") return this.status
    return await this.connect()
  }

  /** Stops the local stack's containers; the workspace was already left. */
  async stopLocal(): Promise<void> {
    if (this.options.store.view.mode !== "local") return
    this.beginAttempt()
    this.fail(connectionFailure("local_stack_stopped"))
    await this.options.surface.showConnectionPage()
    await this.options.local.stop()
    this.notify()
  }

  /** The window's main frame finished a navigation with this HTTP status. */
  mainFrameNavigated(url: string, httpStatus: number): void {
    const origin = this.workspaceOrigin
    if (!origin || !url.startsWith(`${origin}/`)) return
    const failure = describeHttpStatus(httpStatus)
    if (!failure) return
    if (this.phase === "connecting") this.loadFailure = failure
    else void this.failAndShowConnectionPage(failure)
  }

  /** The main frame failed to load after the workspace was showing. */
  mainFrameFailed(description: string): void {
    if (this.phase !== "connected" || isSupersededLoad(description)) return
    void this.failAndShowConnectionPage(describeNetworkError(description))
  }

  /** The workspace tried to leave its server for another origin. */
  redirectRefused(): void {
    if (this.phase === "connecting") this.loadFailure = connectionFailure("redirected")
  }

  rendererGone(): void {
    if (this.phase !== "connected" && this.phase !== "connecting") return
    void this.failAndShowConnectionPage(connectionFailure("page_crashed"))
  }

  dispose(): void {
    this.generation += 1
    this.clearRetry()
    this.removeLocalListener()
    this.listeners.clear()
  }

  private configuredOrigin(mode: ConnectionMode | null): string | null {
    const view = this.options.store.view
    if (mode === "official") return view.officialOrigin
    if (mode === "server") return view.serverOrigin
    if (mode === "local") {
      const port = this.options.local.view.port
      return port ? `http://127.0.0.1:${port}` : null
    }
    return null
  }

  private async resolve(mode: ConnectionMode, generation: number): Promise<Resolution> {
    if (mode !== "local") {
      const origin = this.configuredOrigin(mode)
      if (origin) return { origin }
      this.transition("unconfigured", null)
      return { stop: true }
    }
    const result = await this.options.local.ensureRunning()
    if (generation !== this.generation) return { stop: true }
    switch (result.status) {
      case "ready": return { origin: result.origin }
      case "needs-admin":
        this.transition("local-setup", null)
        return { stop: true }
      case "failed":
        this.fail(result.failure)
        return { stop: true }
      case "cancelled":
        // Only this connection cancels a start, and it begins a new attempt when it does.
        return { stop: true }
    }
  }

  private async open(origin: string, generation: number): Promise<void> {
    const shown = this.status.origin
    this.activeOrigin = origin
    // A local stack may have moved to another port; the page shows where it connects.
    if (shown !== origin) this.notify()
    const probeFailure = await this.options.probe(origin)
    if (generation !== this.generation) return
    if (probeFailure) {
      this.fail(probeFailure)
      return
    }
    this.loadFailure = null
    try {
      await this.options.surface.showWorkspace(workspaceUrl(origin))
    } catch (error) {
      if (generation !== this.generation) return
      // A refused redirect surfaces as an aborted load; its reason was recorded before the abort.
      await this.failAndShowConnectionPage(this.loadFailure ?? describeNetworkError(error instanceof Error ? error.message : String(error)))
      return
    }
    if (generation !== this.generation) return
    if (this.loadFailure) await this.failAndShowConnectionPage(this.loadFailure)
    else if (!this.options.isWorkspaceUrl(this.options.surface.currentUrl(), origin)) await this.failAndShowConnectionPage(connectionFailure("not_agora"))
    else {
      this.retryAttempt = 0
      this.transition("connected", null)
      this.options.onEvent?.("workspace_connected", { mode: this.options.store.view.mode, origin })
    }
  }

  private async failAndShowConnectionPage(failure: ConnectionFailure): Promise<void> {
    this.beginAttempt()
    this.fail(failure)
    await this.options.surface.showConnectionPage()
  }

  private beginAttempt(): number {
    this.generation += 1
    this.activeOrigin = null
    this.clearRetry()
    return this.generation
  }

  private fail(failure: ConnectionFailure): void {
    this.options.onEvent?.("workspace_connection_failed", { code: failure.code })
    if (failure.retryable) {
      const delay = RETRY_DELAYS_MS[Math.min(this.retryAttempt, RETRY_DELAYS_MS.length - 1)]
      this.retryAttempt += 1
      const generation = this.generation
      this.nextRetryAt = this.options.timer.now() + delay
      this.retryHandle = this.options.timer.set(() => {
        this.retryHandle = null
        if (generation === this.generation) void this.connect()
      }, delay)
    }
    this.transition("failed", failure)
  }

  private clearRetry(): void {
    if (this.retryHandle !== null) this.options.timer.clear(this.retryHandle)
    this.retryHandle = null
    this.nextRetryAt = null
  }

  private transition(phase: ConnectionPhase, failure: ConnectionFailure | null): void {
    this.phase = phase
    this.failure = failure
    this.notify()
  }

  private notify(): void {
    const status = this.status
    for (const listener of this.listeners) listener(status)
  }
}

const REGISTRY_IDS: readonly LocalImageRegistryId[] = ["ghcr", "acr"]

/** A choice exactly as the connection page may send it; anything else is refused. */
export function parseChoice(value: unknown): ConnectionChoice | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null
  const record = value as Record<string, unknown>
  if (record.mode === "official") return { mode: "official" }
  if (record.mode === "server") return typeof record.address === "string" ? { mode: "server", address: record.address } : null
  if (record.mode === "local") {
    if (record.registry === undefined) return { mode: "local" }
    return REGISTRY_IDS.includes(record.registry as LocalImageRegistryId) ? { mode: "local", registry: record.registry as LocalImageRegistryId } : null
  }
  return null
}
