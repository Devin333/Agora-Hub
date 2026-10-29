import type { ConnectionFailure } from "./load-failure"

export interface RendererLifecycleState { revision: number; dirty: boolean; activeTaskCount: number; summary?: string }
/** Why the host is about to leave the workspace page: closing it, quitting, switching connection, or stopping the local stack. */
export type QuitReason = "window-close" | "app-quit" | "server-change" | "local-stop"
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
 * Where the workspace comes from:
 * - `official`: the service this build was made for (only when it has one).
 * - `server`: a server the user (or their administrator) runs, by its address.
 * - `local`: a stack the app runs in Docker on this computer.
 */
export type ConnectionMode = "official" | "server" | "local"

/**
 * - `unconfigured`: no connection chosen yet; the connection page asks for one.
 * - `connecting`: the workspace is being prepared (local stack) or loaded.
 * - `connected`: the workspace on `origin` is showing.
 * - `failed`: the last attempt failed for `failure`.
 * - `editing`: the user asked to change the connection from the tray or the page.
 * - `local-setup`: the local stack runs but has no account yet; the page asks for the first administrator.
 */
export type ConnectionPhase = "unconfigured" | "connecting" | "connected" | "failed" | "editing" | "local-setup"

export type LocalStackPhase =
  | "idle"
  | "checking"
  | "preparing"
  | "pulling"
  | "starting"
  | "verifying"
  | "needs-admin"
  | "creating-admin"
  | "ready"
  | "stopping"
  | "stopped"
  | "failed"

export type LocalImageRegistryId = "ghcr" | "acr"

export interface LocalStackView {
  phase: LocalStackPhase
  /** Share of the image download done, 0 to 1, while pulling; null when unknown. */
  progress: number | null
  /** Recent output of the Docker commands, newest last, with secrets removed. */
  output: string[]
  failure: ConnectionFailure | null
  registry: LocalImageRegistryId
  registries: ReadonlyArray<{ id: LocalImageRegistryId; label: string }>
  imageTag: string
  port: number | null
}

export interface ConnectionStatusView {
  phase: ConnectionPhase
  /** The saved choice; null until the user (or the build) made one. */
  mode: ConnectionMode | null
  /** The origin the window connects or is connected to. */
  origin: string | null
  /** The build's official service; null hides that choice. */
  officialOrigin: string | null
  /** The last server address the user saved, offered again in the form. */
  serverOrigin: string | null
  failure: ConnectionFailure | null
  /** Epoch milliseconds of the next automatic attempt, when one is scheduled. */
  nextRetryAt: number | null
  /** Whether a plain-HTTP loopback address is accepted (unpackaged development builds only). */
  allowLoopbackHttp: boolean
  local: LocalStackView
}

export type ConnectionChoice =
  | { mode: "official" }
  | { mode: "server"; address: string }
  | { mode: "local"; registry?: LocalImageRegistryId }

export type ConnectionSaveResult = { ok: true } | { ok: false; code: string; message: string }

export interface LocalAdminRequest { username: string; password: string; confirmation: string }

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
  readonly version: 2
  status(): Promise<ConnectionStatusView>
  retry(): Promise<ConnectionStatusView>
  /** Verifies the chosen connection (starting the local stack for `local`), then saves and opens it. */
  choose(choice: ConnectionChoice): Promise<ConnectionSaveResult>
  /** Opens the connection choices; only offered while the workspace is not showing. */
  edit(): Promise<ConnectionStatusView>
  cancelEdit(): Promise<ConnectionStatusView>
  onChange(listener: (status: ConnectionStatusView) => void): () => void
  local: {
    /** Creates the first administrator of the local stack, then opens its workspace. */
    createAdmin(request: LocalAdminRequest): Promise<ConnectionSaveResult>
    /** Interrupts a local stack start in progress; running containers are left alone. */
    cancel(): Promise<ConnectionStatusView>
  }
}
