import { randomUUID } from "node:crypto"
import type {
  DesktopConfigApplyResult, DesktopConfigEffectScope, DesktopConfigPreviewResult,
  DesktopConfigResult, DesktopConfigView, RuntimeCliResponse
} from "./contracts"
import { RuntimeClient, RuntimeInvocationError } from "./runtime-client"
import type { RuntimeController } from "./runtime-coordinator"

const PREVIEW_TTL_MS = 15 * 60_000
const MAX_PREVIEWS = 16
const IDENTIFIER = /^[A-Za-z][A-Za-z0-9_.:/-]{0,127}$/
const SECRET_URL_PART = /(?:api[-_]?key|access[-_]?token|auth(?:orization)?|bearer|password|secret|token)/i

type ConfigCandidate = Readonly<{
  llm: Readonly<{ route_id: string; base_url: string; model: string }> | null
  source: Readonly<{ api_url: string; timeout_seconds: number }>
}>

interface PreviewReceipt {
  id: string
  candidate: ConfigCandidate
  token: string
  revision: number
  expiresAt: number
  used: boolean
  preview: DesktopConfigPreviewResult
}

export type ConfirmConfigApply = (preview: DesktopConfigPreviewResult) => Promise<boolean>
export type ConfirmFreshLifecycle = () => Promise<boolean>

export interface DesktopConfigController {
  show(): Promise<DesktopConfigResult>
  preview(value: unknown): Promise<DesktopConfigPreviewResult>
  apply(value: unknown, freshLifecycle: ConfirmFreshLifecycle, confirm: ConfirmConfigApply): Promise<DesktopConfigApplyResult>
}

export class UnavailableDesktopConfigBridge implements DesktopConfigController {
  private result(): DesktopConfigResult { return { ok: false, state: "UNKNOWN", code: "desktop_config_bridge_unavailable", message: "桌面配置 bridge 不可用。" } }
  show(): Promise<DesktopConfigResult> { return Promise.resolve(this.result()) }
  preview(): Promise<DesktopConfigPreviewResult> { return Promise.resolve(this.result()) }
  apply(): Promise<DesktopConfigApplyResult> { return Promise.resolve(this.result()) }
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value).sort()
  const expected = [...keys].sort()
  return actual.length === expected.length && actual.every((key, index) => key === expected[index])
}

function cleanUrl(value: unknown, field: string, allowQuery: boolean, stripTrailingSlash = false): string {
  if (typeof value !== "string") throw new Error(`${field}_invalid`)
  const normalized = value.trim()
  if (!normalized || normalized.length > 2048 || /[\u0000-\u001f\u007f]/.test(normalized)) throw new Error(`${field}_invalid`)
  const parsed = new URL(normalized)
  if (!/^https?:$/.test(parsed.protocol) || !parsed.host || parsed.username || parsed.password || parsed.hash || SECRET_URL_PART.test(parsed.pathname + parsed.search) || (!allowQuery && parsed.search)) throw new Error(`${field}_invalid`)
  return stripTrailingSlash ? normalized.replace(/\/+$/, "") : normalized
}

function model(value: unknown): string {
  if (typeof value !== "string") throw new Error("desktop_config_model_invalid")
  const normalized = value.trim()
  if (!normalized || normalized.length > 256 || /[\u0000-\u001f\u007f]/.test(value) || /(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+\S+|-----BEGIN [A-Z ]+PRIVATE KEY-----)/i.test(normalized)) throw new Error("desktop_config_model_invalid")
  return normalized
}

function immutableCandidate(llm: ConfigCandidate["llm"], source: ConfigCandidate["source"]): ConfigCandidate {
  if (llm !== null) Object.freeze(llm)
  Object.freeze(source)
  return Object.freeze({ llm, source })
}

export function validateConfigCandidate(value: unknown): ConfigCandidate {
  const outer = record(value)
  const research = record(outer?.research)
  if (!outer || !hasExactKeys(outer, ["research"]) || !research || !hasExactKeys(research, ["llm", "source"])) throw new Error("desktop_config_candidate_invalid")
  const source = record(research.source)
  if (!source || !hasExactKeys(source, ["apiUrl", "timeoutSeconds"]) || typeof source.timeoutSeconds !== "number" || !Number.isFinite(source.timeoutSeconds) || source.timeoutSeconds < 1 || source.timeoutSeconds > 300) throw new Error("desktop_config_source_invalid")
  const llm = research.llm === null ? null : record(research.llm)
  if (research.llm !== null && (!llm || !hasExactKeys(llm, ["routeId", "baseUrl", "model"]) || typeof llm.routeId !== "string" || !IDENTIFIER.test(llm.routeId.trim()))) throw new Error("desktop_config_llm_invalid")
  return immutableCandidate(
    llm === null ? null : { route_id: (llm.routeId as string).trim(), base_url: cleanUrl(llm.baseUrl, "base_url", false, true), model: model(llm.model) },
    { api_url: cleanUrl(source.apiUrl, "api_url", true), timeout_seconds: source.timeoutSeconds }
  )
}

function cloneCandidate(candidate: ConfigCandidate): Record<string, unknown> {
  return structuredClone(candidate) as Record<string, unknown>
}

function matchesCandidate(view: DesktopConfigView, candidate: ConfigCandidate): boolean {
  const actual = view.research.llm
  return view.research.source.apiUrl === candidate.source.api_url && view.research.source.timeoutSeconds === candidate.source.timeout_seconds &&
    (candidate.llm === null ? actual === null : actual !== null && actual.routeId === candidate.llm.route_id && actual.baseUrl === candidate.llm.base_url && actual.model === candidate.llm.model)
}

function projectedStrings(value: unknown): string[] | undefined {
  if (!Array.isArray(value) || value.length > 32 || !value.every(item => typeof item === "string" && item.length > 0 && item.length <= 128 && IDENTIFIER.test(item))) return undefined
  return [...value]
}

function effectScope(value: unknown): DesktopConfigEffectScope | undefined {
  const item = record(value)
  if (!item || !hasExactKeys(item, ["llm_consumers", "source_consumers", "not_changed"])) return undefined
  const llmConsumers = projectedStrings(item.llm_consumers)
  const sourceConsumers = projectedStrings(item.source_consumers)
  const notChanged = projectedStrings(item.not_changed)
  return llmConsumers && sourceConsumers && notChanged ? { llmConsumers, sourceConsumers, notChanged } : undefined
}

function configuration(value: unknown): DesktopConfigView | undefined {
  try {
    const item = record(value)
    const research = record(item?.research)
    const source = record(research?.source)
    if (!item || !hasExactKeys(item, ["schema", "revision", "research"]) || item.schema !== "newsroom.desktop-runtime-settings/v1" || !Number.isSafeInteger(item.revision) || Number(item.revision) < 0 || !research || !hasExactKeys(research, ["llm", "source"]) || !source || !hasExactKeys(source, ["preset", "api_url", "timeout_seconds"])) return undefined
    if (source.preset !== "arxiv-default" || typeof source.api_url !== "string" || cleanUrl(source.api_url, "api_url", true) !== source.api_url || typeof source.timeout_seconds !== "number" || !Number.isFinite(source.timeout_seconds) || source.timeout_seconds < 1 || source.timeout_seconds > 300) return undefined
    const llm = research.llm === null ? null : record(research.llm)
    if (research.llm !== null && (!llm || !hasExactKeys(llm, ["route_id", "provider", "model", "base_url", "credential_configured", "credential_binding"]) || llm.provider !== "openai-compatible" || typeof llm.route_id !== "string" || !IDENTIFIER.test(llm.route_id) || typeof llm.base_url !== "string" || cleanUrl(llm.base_url, "base_url", false, true) !== llm.base_url || model(llm.model) !== llm.model || (llm.credential_configured !== null && typeof llm.credential_configured !== "boolean") || llm.credential_binding !== "windows-credential-manager")) return undefined
    return {
      schema: "newsroom.desktop-runtime-settings/v1",
      revision: Number(item.revision),
      research: {
        llm: llm === null ? null : {
          routeId: llm.route_id as string,
          baseUrl: llm.base_url as string,
          model: llm.model as string,
          provider: "openai-compatible",
          credentialConfigured: llm.credential_configured as boolean | null,
          credentialBinding: "windows-credential-manager"
        },
        source: { apiUrl: source.api_url, timeoutSeconds: source.timeout_seconds, provider: "arxiv", preset: "arxiv-default" }
      }
    }
  } catch {
    return undefined
  }
}

function baseResult(response: RuntimeCliResponse): DesktopConfigResult {
  if (!response.ok) return { ok: false, state: "UNKNOWN", code: response.code, message: response.message }
  return {
    ok: true,
    state: response.state,
    code: response.code,
    message: response.message,
    configuration: configuration(response.data.configuration),
    effectScope: effectScope(response.data.effect_scope)
  }
}

function failure(error: unknown, code = "desktop_config_bridge_failed"): DesktopConfigResult {
  const invocation = error instanceof RuntimeInvocationError ? error : null
  return { ok: false, state: "UNKNOWN", code: invocation?.code ?? code, message: invocation?.message ?? "桌面运行配置状态未知；未报告成功。" }
}

export class DesktopConfigBridge implements DesktopConfigController {
  private readonly receipts = new Map<string, PreviewReceipt>()
  private applying = false

  // Python remains the sole drain/CAS owner; this controller is refreshed only after
  // an authoritative APPLIED receipt so the renderer cannot keep displaying stale READY.
  constructor(private readonly client: RuntimeClient, private readonly runtime: RuntimeController, private readonly now: () => number = Date.now) {}

  async show(): Promise<DesktopConfigResult> {
    try {
      const response = await this.client.invoke("config-show")
      const result = baseResult(response)
      if (!result.ok) return result
      const expectedState = result.configuration?.research.llm === null ? "UNCONFIGURED" : "CONFIGURED"
      if (!result.configuration || !result.effectScope || response.state !== expectedState) return { ...result, ok: false, state: "UNKNOWN", code: "desktop_config_show_protocol_invalid", message: "运行配置响应缺少可信配置。" }
      return result
    } catch (error) {
      return failure(error)
    }
  }

  async preview(value: unknown): Promise<DesktopConfigPreviewResult> {
    try {
      const candidate = validateConfigCandidate(value)
      const current = await this.show()
      if (!current.ok || !current.configuration) return current
      const response = await this.client.invoke("config-preview", { configCandidate: cloneCandidate(candidate) })
      const projected = baseResult(response)
      if (!response.ok) return projected
      const token = response.data.confirmation_token
      const revision = response.data.base_revision
      const digest = response.data.candidate_digest
      if (response.state !== "CONFIRMATION_REQUIRED" || typeof token !== "string" || !/^desktop-config:[0-9a-f]{32}:[0-9a-f]{16}$/.test(token) || !Number.isSafeInteger(revision) || typeof digest !== "string" || !/^[0-9a-f]{64}$/.test(digest) || !projected.configuration || !projected.effectScope || revision !== current.configuration.revision || projected.configuration.revision !== Number(revision) + 1 || !matchesCandidate(projected.configuration, candidate) || token.slice(-16) !== digest.slice(0, 16)) {
        return { ok: false, state: "UNKNOWN", code: "desktop_config_preview_protocol_invalid", message: "配置预览回执无效。" }
      }
      this.prune()
      const id = randomUUID()
      const expiresAt = this.now() + PREVIEW_TTL_MS
      const preview: DesktopConfigPreviewResult = { ...projected, previewId: id, current: structuredClone(current.configuration), expiresAt: new Date(expiresAt).toISOString() }
      if (this.receipts.size >= MAX_PREVIEWS) this.receipts.delete(this.receipts.keys().next().value as string)
      this.receipts.set(id, { id, candidate, token, revision: Number(revision), expiresAt, used: false, preview: structuredClone(preview) })
      return structuredClone(preview)
    } catch (error) {
      return failure(error, "desktop_config_candidate_invalid")
    }
  }

  async apply(value: unknown, freshLifecycle: ConfirmFreshLifecycle, confirm: ConfirmConfigApply): Promise<DesktopConfigApplyResult> {
    const request = record(value)
    const id = request && hasExactKeys(request, ["previewId"]) && typeof request.previewId === "string" ? request.previewId : ""
    this.prune()
    const receipt = this.receipts.get(id)
    const conflict = (): DesktopConfigApplyResult => ({ ok: false, state: "CONFLICT", code: "desktop_config_preview_invalid", message: "配置预览已过期、已使用或 revision 已变化；请重新预览。" })
    const cancelled = (): DesktopConfigApplyResult => ({ ok: false, state: "CANCELLED", code: "desktop_config_cancelled", message: "已取消；未停止服务或应用配置。", cancelled: true })
    if (!receipt || receipt.used) return conflict()
    if (this.applying) return { ok: false, state: "BLOCKED", code: "desktop_config_apply_busy", message: "另一个配置事务正在核对或应用。" }
    this.applying = true
    try {
      const current = await this.show()
      if (!current.ok || !current.configuration) return current
      if (current.configuration.revision !== receipt.revision || receipt.expiresAt <= this.now()) return conflict()
      if (matchesCandidate(current.configuration, receipt.candidate)) return { ...current, ok: false, code: "desktop_config_no_change", message: "候选与当前配置相同；未停止服务。" }
      if (!await freshLifecycle() || !await confirm(structuredClone(receipt.preview))) return cancelled()
      // Native confirmation can outlive renderer state. Re-query without clearing dirty/task state.
      if (!await freshLifecycle()) return cancelled()
      if (receipt.expiresAt <= this.now() || receipt.used || this.receipts.get(id) !== receipt) return conflict()
      const fresh = await this.show()
      if (!fresh.ok || !fresh.configuration) return fresh
      if (fresh.configuration.revision !== receipt.revision || receipt.expiresAt <= this.now()) return conflict()
      // The Python owner repeats admission before owner drain, then CAS under its lock.
      receipt.used = true
      const response = await this.client.invoke("config-apply", {
        configCandidate: cloneCandidate(receipt.candidate),
        expectedRevision: receipt.revision,
        confirmationToken: receipt.token
      })
      const result = baseResult(response)
      if (!response.ok) return result
      if (response.state !== "APPLIED" || response.data.restart_required !== true || !result.configuration || result.configuration.revision !== receipt.revision + 1 || !matchesCandidate(result.configuration, receipt.candidate) || !result.effectScope) {
        return { ok: false, state: "UNKNOWN", code: "desktop_config_apply_protocol_invalid", message: "配置事务回执无法确认；请重新读取配置，不得假称成功。" }
      }
      const runtime = await this.runtime.refresh().catch(() => undefined)
      return { ...result, ok: true, restartRequired: true, ...(runtime ? { runtime } : {}) }
    } catch (error) {
      return failure(error)
    } finally {
      this.applying = false
    }
  }

  private prune(): void {
    const now = this.now()
    for (const [id, receipt] of this.receipts) if (receipt.expiresAt <= now || receipt.used) this.receipts.delete(id)
    while (this.receipts.size > MAX_PREVIEWS) this.receipts.delete(this.receipts.keys().next().value as string)
  }
}
