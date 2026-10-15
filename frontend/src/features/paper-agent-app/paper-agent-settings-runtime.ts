"use client"

import { z } from "zod"

const runtimePhaseSchema = z.enum(["starting", "ready", "stopped", "degraded", "error", "unknown"])
const runtimeStatusSchema = z.object({
  phase: runtimePhaseSchema,
  state: z.string().max(80),
  code: z.string().max(120),
  message: z.string().max(500),
  canonicalOrigin: z.literal("http://127.0.0.1:3120").nullable(),
  instanceKey: z.string().max(200).nullable(),
  localTransportReady: z.boolean(),
  canRetry: z.boolean(),
  checkedAt: z.string().max(100),
}).strict()
const runtimeEventSchema = z.object({
  at: z.string().max(100),
  command: z.enum(["start", "status", "stop", "doctor", "backup", "restore", "config-show", "config-preview", "config-apply", "credential-inspect", "credential-prompt", "credential-delete", "data-clear-preview", "data-clear-apply", "data-clear-status", "data-clear-finalize", "host"]),
  state: z.string().max(80),
  code: z.string().max(120),
  message: z.string().max(500),
}).strict()
const runtimeDiagnosticsSchema = z.object({ hostVersion: z.string().max(100), packaged: z.boolean(), runtime: runtimeStatusSchema, recentEvents: z.array(runtimeEventSchema).max(25) }).strict()
const maintenanceBase = { ok: z.boolean(), code: z.string().min(1).max(160), message: z.string().max(1000), state: z.string().max(100), cancelled: z.boolean().optional() }
const backupResultSchema = z.object({ ...maintenanceBase, backup: z.object({ backupId: z.string().max(256).nullable(), fileName: z.string().min(1).max(260), fileCount: z.number().int().nonnegative().nullable(), totalBytes: z.number().int().nonnegative().nullable() }).strict().optional() }).strict()
const personalMemoryRestoreSchema = z.object({ backupId: z.string().min(1).max(256), statusCounts: z.record(z.string().max(80), z.number().int().nonnegative()), policyCount: z.number().int().nonnegative(), requiresExplicitActivationConfirmation: z.literal(true), warning: z.string().max(1000) }).strict()
const restorePreviewSchema = z.object({ ...maintenanceBase, previewId: z.string().uuid().optional(), backupId: z.string().max(256).optional(), isolationVerified: z.boolean().optional(), personalMemory: personalMemoryRestoreSchema.optional(), expiresAt: z.string().max(100).optional() }).strict()
const restoreActivateSchema = z.object({ ...maintenanceBase, backupId: z.string().max(256).optional() }).strict()

const llmCandidateSchema = z.object({ routeId: z.string().min(1).max(128), baseUrl: z.string().min(1).max(2048), model: z.string().min(1).max(256) }).strict()
const sourceCandidateSchema = z.object({ apiUrl: z.string().min(1).max(2048), timeoutSeconds: z.number().min(1).max(300) }).strict()
const configCandidateSchema = z.object({ research: z.object({ llm: llmCandidateSchema.nullable(), source: sourceCandidateSchema }).strict() }).strict()
const configLlmViewSchema = llmCandidateSchema.extend({ provider: z.literal("openai-compatible"), credentialConfigured: z.boolean().nullable(), credentialBinding: z.literal("windows-credential-manager") }).strict()
const configViewSchema = z.object({
  schema: z.literal("newsroom.desktop-runtime-settings/v1"),
  revision: z.number().int().nonnegative(),
  research: z.object({ llm: configLlmViewSchema.nullable(), source: sourceCandidateSchema.extend({ provider: z.literal("arxiv"), preset: z.literal("arxiv-default") }).strict() }).strict(),
}).strict()
const effectScopeSchema = z.object({ llmConsumers: z.array(z.string().min(1).max(160)).max(20), sourceConsumers: z.array(z.string().min(1).max(160)).max(20), notChanged: z.array(z.string().min(1).max(160)).max(20) }).strict()
const configResultFields = { ok: z.boolean(), state: z.string().max(100), code: z.string().min(1).max(160), message: z.string().max(1000), configuration: configViewSchema.optional(), effectScope: effectScopeSchema.optional() }
const configResultSchema = z.object(configResultFields).strict()
const configPreviewResultSchema = z.object({ ...configResultFields, previewId: z.string().min(1).max(512).optional(), current: configViewSchema.optional(), expiresAt: z.string().max(100).optional() }).strict()
const configApplyResultSchema = z.object({ ...configResultFields, cancelled: z.boolean().optional(), restartRequired: z.boolean().optional(), runtime: runtimeStatusSchema.optional() }).strict()
const credentialResultSchema = z.object({ ok: z.boolean(), code: z.string().min(1).max(160), configured: z.boolean().nullable(), changed: z.boolean().optional(), cancelled: z.boolean().optional(), restartRequired: z.boolean().optional(), state: z.enum(["READY", "STOPPED", "UNKNOWN"]).optional() }).strict()

export type DesktopRuntimeStatus = z.infer<typeof runtimeStatusSchema>
export type DesktopRuntimeDiagnostics = z.infer<typeof runtimeDiagnosticsSchema>
export type DesktopBackupResult = z.infer<typeof backupResultSchema>
export type DesktopRestorePreview = z.infer<typeof restorePreviewSchema>
export type DesktopRestoreActivateResult = z.infer<typeof restoreActivateSchema>
export type DesktopConfigCandidate = z.infer<typeof configCandidateSchema>
export type DesktopConfigView = z.infer<typeof configViewSchema>
export type DesktopConfigResult = z.infer<typeof configResultSchema>
export type DesktopConfigPreviewResult = z.infer<typeof configPreviewResultSchema>
export type DesktopConfigApplyResult = z.infer<typeof configApplyResultSchema>
export type DesktopCredentialResult = z.infer<typeof credentialResultSchema>
export type DesktopHostSnapshot = { kind: "browser" } | { kind: "desktop"; status: DesktopRuntimeStatus; diagnostics: DesktopRuntimeDiagnostics }

export type DesktopConfigDraft = {
  llmEnabled: boolean
  routeId: string
  model: string
  baseUrl: string
  sourceApiUrl: string
  sourceTimeoutSeconds: string
}
export type DesktopConfigDraftErrors = Partial<Record<keyof DesktopConfigDraft, string>>

const ROUTE_ID = /^[A-Za-z][A-Za-z0-9_.:/-]{0,127}$/
export function configDraftFromView(view: DesktopConfigView): DesktopConfigDraft {
  return {
    llmEnabled: view.research.llm !== null,
    routeId: view.research.llm?.routeId ?? "writer-primary",
    model: view.research.llm?.model ?? "",
    baseUrl: view.research.llm?.baseUrl ?? "",
    sourceApiUrl: view.research.source.apiUrl,
    sourceTimeoutSeconds: String(view.research.source.timeoutSeconds),
  }
}
export function validateDesktopConfigDraft(draft: DesktopConfigDraft): { candidate?: DesktopConfigCandidate; errors: DesktopConfigDraftErrors } {
  const errors: DesktopConfigDraftErrors = {}
  const routeId = draft.routeId.trim()
  const model = draft.model.trim()
  const baseUrl = draft.baseUrl.trim().replace(/\/+$/, "")
  const sourceApiUrl = draft.sourceApiUrl.trim()
  const timeoutSeconds = Number(draft.sourceTimeoutSeconds)
  if (draft.llmEnabled) {
    if (!ROUTE_ID.test(routeId)) errors.routeId = "路由 ID 必须以字母开头，且只能包含字母、数字、._:/-。"
    if (!model || model.length > 256 || /[\u0000-\u001f]/.test(model)) errors.model = "请输入不超过 256 个字符的模型名称。"
    if (!validHttpUrl(baseUrl, false)) errors.baseUrl = "请输入不含凭据、查询或片段的 HTTP(S) API 地址。"
  }
  if (!validHttpUrl(sourceApiUrl, true)) errors.sourceApiUrl = "请输入不含凭据或片段的 HTTP(S) Arxiv API 地址。"
  if (!Number.isFinite(timeoutSeconds) || timeoutSeconds < 1 || timeoutSeconds > 300) errors.sourceTimeoutSeconds = "超时必须是 1 到 300 秒之间的数字。"
  if (Object.keys(errors).length) return { errors }
  return { errors, candidate: configCandidateSchema.parse({ research: { llm: draft.llmEnabled ? { routeId, model, baseUrl } : null, source: { apiUrl: sourceApiUrl, timeoutSeconds } } }) }
}
function validHttpUrl(value: string, allowQuery: boolean): boolean {
  try {
    const parsed = new URL(value)
    return (parsed.protocol === "http:" || parsed.protocol === "https:") && Boolean(parsed.hostname) && !parsed.username && !parsed.password && !parsed.hash && (allowQuery || !parsed.search)
  } catch { return false }
}

interface DesktopRuntimeBridge { status(): Promise<unknown>; retry(): Promise<unknown>; diagnostics(): Promise<unknown>; backup?: () => Promise<unknown>; restore?: { preview?: () => Promise<unknown>; activate?: (request: { previewId: string }) => Promise<unknown> } }
interface DesktopConfigBridge { show(): Promise<unknown>; preview(candidate: DesktopConfigCandidate): Promise<unknown>; apply(request: { previewId: string }): Promise<unknown> }
interface DesktopCredentialBridge { status(): Promise<unknown>; replace(): Promise<unknown>; remove(): Promise<unknown> }
type DesktopWindow = Window & { agoraDesktop?: { version?: unknown; runtime?: DesktopRuntimeBridge; config?: DesktopConfigBridge; credentials?: DesktopCredentialBridge } }

export function hasDesktopRuntimeBridge(): boolean { return getDesktopRuntimeBridge() !== null }
export function hasDesktopMaintenanceBridge(): boolean { return getDesktopMaintenanceBridge() !== null }
export function hasDesktopConfigBridge(): boolean { return getDesktopConfigBridge() !== null }
export function hasDesktopCredentialBridge(): boolean { return getDesktopCredentialBridge() !== null }
export async function readDesktopHostSnapshot(): Promise<DesktopHostSnapshot> { const bridge = getDesktopRuntimeBridge(); if (!bridge) return { kind: "browser" }; return { kind: "desktop", status: runtimeStatusSchema.parse(await bridge.status()), diagnostics: runtimeDiagnosticsSchema.parse(await bridge.diagnostics()) } }
export async function retryDesktopHostRuntime(): Promise<DesktopHostSnapshot> { const bridge = getDesktopRuntimeBridge(); if (!bridge) return { kind: "browser" }; return { kind: "desktop", status: runtimeStatusSchema.parse(await bridge.retry()), diagnostics: runtimeDiagnosticsSchema.parse(await bridge.diagnostics()) } }
export async function createDesktopBackup(): Promise<DesktopBackupResult> { const bridge = requireDesktopMaintenanceBridge(); return backupResultSchema.parse(await bridge.backup()) }
export async function previewDesktopRestore(): Promise<DesktopRestorePreview> { const bridge = requireDesktopMaintenanceBridge(); return restorePreviewSchema.parse(await bridge.restore.preview()) }
export async function activateDesktopRestore(previewId: string): Promise<DesktopRestoreActivateResult> { const bridge = requireDesktopMaintenanceBridge(); return restoreActivateSchema.parse(await bridge.restore.activate({ previewId })) }
export async function showDesktopConfig(): Promise<DesktopConfigResult> { return configResultSchema.parse(await requireDesktopConfigBridge().show()) }
export async function previewDesktopConfig(candidate: DesktopConfigCandidate): Promise<DesktopConfigPreviewResult> { return configPreviewResultSchema.parse(await requireDesktopConfigBridge().preview(configCandidateSchema.parse(candidate))) }
export async function applyDesktopConfig(previewId: string): Promise<DesktopConfigApplyResult> { return configApplyResultSchema.parse(await requireDesktopConfigBridge().apply({ previewId })) }
export async function inspectDesktopCredential(): Promise<DesktopCredentialResult> { return credentialResultSchema.parse(await requireDesktopCredentialBridge().status()) }
export async function replaceDesktopCredential(): Promise<DesktopCredentialResult> { return credentialResultSchema.parse(await requireDesktopCredentialBridge().replace()) }
export async function removeDesktopCredential(): Promise<DesktopCredentialResult> { return credentialResultSchema.parse(await requireDesktopCredentialBridge().remove()) }

function desktopApi() { if (typeof window === "undefined") return null; const desktop = (window as DesktopWindow).agoraDesktop; return desktop?.version === 1 ? desktop : null }
function getDesktopRuntimeBridge(): DesktopRuntimeBridge | null { const runtime = desktopApi()?.runtime; return runtime && typeof runtime.status === "function" && typeof runtime.retry === "function" && typeof runtime.diagnostics === "function" ? runtime : null }
function getDesktopMaintenanceBridge() { const runtime = getDesktopRuntimeBridge(); return runtime && typeof runtime.backup === "function" && typeof runtime.restore?.preview === "function" && typeof runtime.restore.activate === "function" ? { backup: runtime.backup, restore: { preview: runtime.restore.preview, activate: runtime.restore.activate } } : null }
function getDesktopConfigBridge(): DesktopConfigBridge | null { const config = desktopApi()?.config; return config && typeof config.show === "function" && typeof config.preview === "function" && typeof config.apply === "function" ? config : null }
function getDesktopCredentialBridge(): DesktopCredentialBridge | null { const credentials = desktopApi()?.credentials; return credentials && typeof credentials.status === "function" && typeof credentials.replace === "function" && typeof credentials.remove === "function" ? credentials : null }
function requireDesktopMaintenanceBridge() { const bridge = getDesktopMaintenanceBridge(); if (!bridge) throw new Error("desktop_maintenance_unavailable"); return bridge }
function requireDesktopConfigBridge() { const bridge = getDesktopConfigBridge(); if (!bridge) throw new Error("desktop_config_unavailable"); return bridge }
function requireDesktopCredentialBridge() { const bridge = getDesktopCredentialBridge(); if (!bridge) throw new Error("desktop_credential_unavailable"); return bridge }
