import type { ConnectionFailure } from "./load-failure"
import type { ServerOriginSource } from "./server-connection"

export interface RendererLifecycleState { revision: number; dirty: boolean; activeTaskCount: number; summary?: string }
/** Why the host is about to leave the workspace page: closing it, quitting, or switching to another server. */
export type QuitReason = "window-close" | "app-quit" | "server-change"
export interface QuitRequest { requestId: string; reason: QuitReason; state: RendererLifecycleState }
export interface QuitResponse { requestId: string; decision: "allow" | "cancel"; state: RendererLifecycleState }

export type ExportFormat = "txt" | "md" | "json"
export interface ExportRequest { suggestedName: string; format: ExportFormat; content: string }
export interface ExportResult { ok: boolean; code: "saved" | "cancelled" | "invalid_input" | "unsafe_target" | "write_failed"; fileName?: string; message: string }

export type AppearancePreference = "system" | "light" | "dark"
export type ResolvedTheme = "light" | "dark"
/** The preference the host applied and the palette it resolves to right now. */
export interface AppearanceView { preference: AppearancePreference; theme: ResolvedTheme }

/**
 * - `unconfigured`: no server address yet; the connection page asks for one.
 * - `connecting`: the workspace on `origin` is loading.
 * - `connected`: the workspace on `origin` is showing.
 * - `failed`: the last attempt failed for `failure`.
 * - `editing`: the user asked to change the address from the tray.
 */
export type ConnectionPhase = "unconfigured" | "connecting" | "connected" | "failed" | "editing"

export interface ConnectionStatusView {
  phase: ConnectionPhase
  origin: string | null
  source: ServerOriginSource | null
  bundledOrigin: string | null
  failure: ConnectionFailure | null
  /** Epoch milliseconds of the next automatic attempt, when one is scheduled. */
  nextRetryAt: number | null
  /** Whether a plain-HTTP loopback address is accepted (unpackaged development builds only). */
  allowLoopbackHttp: boolean
}

export type ConnectionSaveResult = { ok: true } | { ok: false; code: string; message: string }

/** Bridge of the remote workspace page (`window.agoraDesktop`). */
export interface AgoraDesktopApi {
  readonly version: 1
  lifecycle: {
    updateState(state: RendererLifecycleState): Promise<{ accepted: boolean; revision: number }>
    onQuitRequested(listener: (request: QuitRequest) => void): () => void
    respondToQuit(response: QuitResponse): Promise<{ accepted: boolean }>
  }
  export: { save(request: ExportRequest): Promise<ExportResult> }
  appearance: { set(preference: AppearancePreference): Promise<AppearanceView> }
}

/** Bridge of the packaged connection page (`window.agoraConnection`). */
export interface AgoraConnectionApi {
  readonly version: 1
  status(): Promise<ConnectionStatusView>
  retry(): Promise<ConnectionStatusView>
  save(address: string): Promise<ConnectionSaveResult>
  reset(): Promise<ConnectionSaveResult>
  /** Opens the address form; only offered while the workspace is not showing. */
  edit(): Promise<ConnectionStatusView>
  cancelEdit(): Promise<ConnectionStatusView>
  onChange(listener: (status: ConnectionStatusView) => void): () => void
}
