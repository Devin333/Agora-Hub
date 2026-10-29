import type { ConnectionPhase, ConnectionSaveResult, ConnectionStatusView } from "./contracts"
import { connectionFailure, describeHttpStatus, describeNetworkError, isSupersededLoad, type ConnectionFailure } from "./load-failure"
import type { ServerConnectionStore } from "./server-connection"
import { parseServerOrigin, serverOriginErrorMessage } from "./server-origin"

/**
 * Drives the one window between the packaged connection page and the
 * workspace served by the configured server.
 *
 * Every attempt first probes the server, then loads the workspace; a failure at
 * either step, or a later failure of the loaded workspace, brings the
 * connection page back with a fixed explanation and, for transient causes, an
 * automatic retry with growing delays. Each attempt carries a generation, so a
 * result that arrives after a newer attempt started is ignored.
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

export interface WorkspaceConnectionOptions {
  store: ServerConnectionStore
  surface: WorkspaceSurface
  probe(origin: string): Promise<ConnectionFailure | null>
  timer: RetryTimer
  isWorkspaceUrl(url: string, origin: string): boolean
  onEvent?(event: string, details: Record<string, unknown>): void
}

export const RETRY_DELAYS_MS: readonly number[] = [2_000, 5_000, 10_000, 30_000, 60_000]
export const WORKSPACE_PATH = "/paper-agent"

export function workspaceUrl(origin: string): string {
  return new URL(WORKSPACE_PATH, origin).href
}

export class WorkspaceConnection {
  private phase: ConnectionPhase = "unconfigured"
  private failure: ConnectionFailure | null = null
  private generation = 0
  private retryHandle: unknown = null
  private nextRetryAt: number | null = null
  private retryAttempt = 0
  /** A failure observed while the workspace was still loading, reported once the load settles. */
  private loadFailure: ConnectionFailure | null = null
  private readonly listeners = new Set<(status: ConnectionStatusView) => void>()

  constructor(private readonly options: WorkspaceConnectionOptions) {}

  get status(): ConnectionStatusView {
    const view = this.options.store.view
    return {
      phase: this.phase,
      origin: view.origin,
      source: view.source,
      bundledOrigin: view.bundledOrigin,
      failure: this.failure,
      nextRetryAt: this.nextRetryAt,
      allowLoopbackHttp: this.options.store.policy.allowLoopbackHttp
    }
  }

  /** The origin whose pages may load and use the workspace bridge right now. */
  get workspaceOrigin(): string | null {
    return this.phase === "connecting" || this.phase === "connected" ? this.options.store.view.origin : null
  }

  get connected(): boolean {
    return this.phase === "connected"
  }

  onChange(listener: (status: ConnectionStatusView) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  /** Starts (or restarts) an attempt against the configured server. */
  async connect(): Promise<ConnectionStatusView> {
    const generation = this.beginAttempt()
    const origin = this.options.store.view.origin
    if (!origin) {
      this.transition("unconfigured", null)
      await this.options.surface.showConnectionPage()
      return this.status
    }
    this.transition("connecting", null)
    // The page shows the attempt's progress; the surface leaves it alone when it is already showing.
    await this.options.surface.showConnectionPage()
    const probeFailure = await this.options.probe(origin)
    if (generation !== this.generation) return this.status
    if (probeFailure) {
      this.fail(probeFailure)
      return this.status
    }
    this.loadFailure = null
    try {
      await this.options.surface.showWorkspace(workspaceUrl(origin))
    } catch (error) {
      if (generation !== this.generation) return this.status
      // A refused redirect surfaces as an aborted load; its reason was recorded before the abort.
      await this.failAndShowConnectionPage(this.loadFailure ?? describeNetworkError(error instanceof Error ? error.message : String(error)))
      return this.status
    }
    if (generation !== this.generation) return this.status
    if (this.loadFailure) await this.failAndShowConnectionPage(this.loadFailure)
    else if (!this.options.isWorkspaceUrl(this.options.surface.currentUrl(), origin)) await this.failAndShowConnectionPage(connectionFailure("not_agora"))
    else {
      this.retryAttempt = 0
      this.transition("connected", null)
      this.options.onEvent?.("workspace_connected", { origin })
    }
    return this.status
  }

  /** A retry the user asked for, or the connection page's reaction to the network coming back. */
  async retry(): Promise<ConnectionStatusView> {
    // An attempt in flight or a shown workspace needs no retry; an open address form waits for the user.
    if (this.phase === "connected" || this.phase === "connecting" || this.phase === "editing") return this.status
    this.retryAttempt = 0
    return await this.connect()
  }

  /** Validates, verifies and stores a new address, then connects to it. */
  async save(address: unknown): Promise<ConnectionSaveResult> {
    const parsed = parseServerOrigin(address, this.options.store.policy)
    if (!parsed.ok) return { ok: false, code: parsed.code, message: serverOriginErrorMessage(parsed.code) }
    // A wrong address is rejected here rather than saved and failed against later.
    const failure = await this.options.probe(parsed.origin)
    if (failure) return { ok: false, code: failure.code, message: failure.message }
    await this.options.store.save(parsed.origin)
    this.options.onEvent?.("server_address_saved", { origin: parsed.origin })
    this.retryAttempt = 0
    void this.connect()
    return { ok: true }
  }

  async reset(): Promise<ConnectionSaveResult> {
    await this.options.store.reset()
    this.options.onEvent?.("server_address_reset", {})
    this.retryAttempt = 0
    void this.connect()
    return { ok: true }
  }

  /** Leaves the workspace to let the user enter another address; the caller already settled unsaved work. */
  async edit(): Promise<void> {
    this.beginAttempt()
    this.transition("editing", null)
    await this.options.surface.showConnectionPage()
  }

  async cancelEdit(): Promise<ConnectionStatusView> {
    if (this.phase !== "editing") return this.status
    return await this.connect()
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
    this.listeners.clear()
  }

  private async failAndShowConnectionPage(failure: ConnectionFailure): Promise<void> {
    this.beginAttempt()
    this.fail(failure)
    await this.options.surface.showConnectionPage()
  }

  private beginAttempt(): number {
    this.generation += 1
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
    const status = this.status
    for (const listener of this.listeners) listener(status)
  }
}
