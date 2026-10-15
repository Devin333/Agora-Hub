export const RUNTIME_COMMANDS = ["start", "status", "stop", "doctor", "backup", "restore", "config-show", "config-preview", "config-apply", "credential-inspect", "credential-prompt", "credential-delete", "data-clear-preview", "data-clear-apply", "data-clear-status", "data-clear-finalize"] as const
export type RuntimeCommand = (typeof RUNTIME_COMMANDS)[number]

export type RuntimePhase = "starting" | "ready" | "stopped" | "degraded" | "error" | "unknown"

export interface RuntimeCliData { canonical_origin?: string; instance_key?: string; [key: string]: unknown }
export interface RuntimeCliResponse { ok: boolean; command: RuntimeCommand; state: string; code: string; message: string; data: RuntimeCliData }
export interface RuntimeStatusView { phase: RuntimePhase; state: string; code: string; message: string; canonicalOrigin: string | null; instanceKey: string | null; localTransportReady: boolean; canRetry: boolean; checkedAt: string }
export interface RuntimeDiagnosticEvent { at: string; command: RuntimeCommand | "host"; state: string; code: string; message: string }
export interface RuntimeDiagnosticsView { hostVersion: string; packaged: boolean; runtime: RuntimeStatusView; recentEvents: RuntimeDiagnosticEvent[] }
export interface RuntimeBackupSummary { backupId: string | null; fileName: string; fileCount: number | null; totalBytes: number | null }
export interface PersonalMemoryRestoreSummary { backupId: string; statusCounts: Record<string, number>; policyCount: number; requiresExplicitActivationConfirmation: true; warning: string }
export interface RuntimeMaintenanceResult { ok: boolean; code: string; message: string; state: string; cancelled?: boolean }
export interface RuntimeBackupResult extends RuntimeMaintenanceResult { backup?: RuntimeBackupSummary }
export interface RestorePreviewResult extends RuntimeMaintenanceResult { previewId?: string; backupId?: string; isolationVerified?: boolean; personalMemory?: PersonalMemoryRestoreSummary; expiresAt?: string }
export interface RestoreActivateRequest { previewId: string }
export interface RestoreActivateResult extends RuntimeMaintenanceResult { backupId?: string }
export interface DataClearScope {
  profilePath: string
  fileCount: number
  totalBytes: number
  includes: string[]
  preserves: string[]
  credentialPolicy: string
}
export interface DataClearResultBase { ok: boolean; state: string; code: string; message: string; cancelled?: boolean }
export interface DataClearPreviewResult extends DataClearResultBase { previewId?: string; expiresAt?: string; scope?: DataClearScope; stopped?: boolean }
export interface DataClearApplyRequest { previewId: string; confirmationText: string }
export interface DataClearApplyResult extends DataClearResultBase { operationId?: string; exitRequired?: boolean }
export interface DataClearStatusResult extends DataClearResultBase { operationId?: string; completedAt?: string }
export const DATA_CLEAR_CONFIRMATION_TEXT = "删除本机全部 Agora 数据" as const

export interface RendererLifecycleState { revision: number; dirty: boolean; activeTaskCount: number; summary?: string }
export interface QuitRequest { requestId: string; reason: "window-close" | "app-quit" | "config-apply" | "data-clear"; state: RendererLifecycleState }
export interface QuitResponse { requestId: string; decision: "allow" | "cancel"; state: RendererLifecycleState }

export type ExportFormat = "txt" | "md" | "json"
export interface ExportRequest { suggestedName: string; format: ExportFormat; content: string }
export interface ExportResult { ok: boolean; code: "saved" | "cancelled" | "invalid_input" | "unsafe_target" | "write_failed"; fileName?: string; message: string }

export interface DesktopResearchLlmCandidate { routeId: string; baseUrl: string; model: string }
export interface DesktopResearchSourceCandidate { apiUrl: string; timeoutSeconds: number }
export interface DesktopConfigCandidate { research: { llm: DesktopResearchLlmCandidate | null; source: DesktopResearchSourceCandidate } }
export interface DesktopConfigLlmView extends DesktopResearchLlmCandidate { provider: "openai-compatible"; credentialConfigured: boolean | null; credentialBinding: "windows-credential-manager" }
export interface DesktopConfigView { schema: "newsroom.desktop-runtime-settings/v1"; revision: number; research: { llm: DesktopConfigLlmView | null; source: DesktopResearchSourceCandidate & { provider: "arxiv"; preset: string } } }
export interface DesktopConfigEffectScope { llmConsumers: string[]; sourceConsumers: string[]; notChanged: string[] }
export interface DesktopConfigResult { ok: boolean; state: string; code: string; message: string; configuration?: DesktopConfigView; effectScope?: DesktopConfigEffectScope }
export interface DesktopConfigPreviewResult extends DesktopConfigResult { previewId?: string; current?: DesktopConfigView; expiresAt?: string }
export interface DesktopConfigApplyRequest { previewId: string }
export interface DesktopConfigApplyResult extends DesktopConfigResult { cancelled?: boolean; restartRequired?: boolean; runtime?: RuntimeStatusView }
export interface DesktopCredentialResult { ok: boolean; code: string; configured: boolean | null; changed?: boolean; cancelled?: boolean; restartRequired?: boolean; state?: "READY" | "STOPPED" | "UNKNOWN" }

export interface AgoraDesktopApi {
  readonly version: 1
  runtime: {
    status(): Promise<RuntimeStatusView>; retry(): Promise<RuntimeStatusView>; diagnostics(): Promise<RuntimeDiagnosticsView>; backup(): Promise<RuntimeBackupResult>
    restore: { preview(): Promise<RestorePreviewResult>; activate(request: RestoreActivateRequest): Promise<RestoreActivateResult> }
    clearData: { preview(): Promise<DataClearPreviewResult>; apply(request: DataClearApplyRequest): Promise<DataClearApplyResult>; status(): Promise<DataClearStatusResult> }
  }
  config: {
    show(): Promise<DesktopConfigResult>
    preview(candidate: DesktopConfigCandidate): Promise<DesktopConfigPreviewResult>
    apply(request: DesktopConfigApplyRequest): Promise<DesktopConfigApplyResult>
  }
  credentials: { status(): Promise<DesktopCredentialResult>; replace(): Promise<DesktopCredentialResult>; remove(): Promise<DesktopCredentialResult> }
  lifecycle: {
    updateState(state: RendererLifecycleState): Promise<{ accepted: boolean; revision: number }>
    onQuitRequested(listener: (request: QuitRequest) => void): () => void
    respondToQuit(response: QuitResponse): Promise<{ accepted: boolean }>
  }
  export: { save(request: ExportRequest): Promise<ExportResult> }
}



