import type {
  CatalogModel,
  CatalogProvider,
  CustomProvider,
  ModelConnection,
  ModelConnectionInput,
  ModelConnectionsView,
  ModelProvider,
  ReasoningEffort,
} from "@/lib/account/model-connections"

// Form state of one model connection and the rules the form shares with the
// API: which fields a provider takes and when the key must be typed again.
// Whether a draft fits a purpose is asked of the API (usePurposeFitPreview),
// never computed here.

export type ConnectionDraft = {
  providerId: string
  model: string
  label: string
  // "" sends nothing and keeps the provider's own default tier.
  reasoningEffort: ReasoningEffort | ""
  apiKey: string
  baseUrl: string
  contextWindowTokens: string
  maxOutputTokens: string
  reasoningParameter: string
  structuredOutput: string
}

export const EMPTY_DRAFT: ConnectionDraft = {
  providerId: "",
  model: "",
  label: "",
  reasoningEffort: "",
  apiKey: "",
  baseUrl: "",
  contextWindowTokens: "",
  maxOutputTokens: "",
  reasoningParameter: "none",
  structuredOutput: "json_object",
}

export const EFFORT_LABELS: Record<ReasoningEffort, string> = {
  off: "关闭思考",
  minimal: "极低",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "很高",
  max: "最高",
}

export const REASONING_PARAMETER_LABELS: Record<string, string> = {
  none: "不发送思考参数",
  openai_reasoning_effort: "OpenAI reasoning_effort",
  openrouter_reasoning: "OpenRouter reasoning.effort",
  deepseek_thinking: "DeepSeek thinking",
  dashscope_enable_thinking: "百炼 enable_thinking",
  anthropic_effort: "Anthropic output_config.effort",
}

export const STRUCTURED_OUTPUT_LABELS: Record<string, string> = {
  json_object: "支持 JSON 对象（response_format: json_object）",
  none: "不支持，只用提示词约束格式",
}

export function providerById(view: ModelConnectionsView, providerId: string): ModelProvider | null {
  return view.catalog.providers.find(item => item.id === providerId) ?? null
}

export function catalogModel(provider: ModelProvider | null, modelId: string): CatalogModel | null {
  return provider?.kind === "catalog" ? provider.models.find(item => item.id === modelId) ?? null : null
}

/** A blank draft for a provider: its default model and nothing else chosen. */
export function draftForProvider(provider: ModelProvider | null): ConnectionDraft {
  if (!provider) return EMPTY_DRAFT
  if (provider.kind === "catalog") return { ...EMPTY_DRAFT, providerId: provider.id, model: provider.defaultModel }
  return {
    ...EMPTY_DRAFT,
    providerId: provider.id,
    reasoningParameter: provider.reasoningParameters[0] ?? "none",
    structuredOutput: provider.structuredOutputModes[0] ?? "none",
  }
}

/** The form state that shows a stored connection; the key is never prefilled. */
export function draftFromConnection(connection: ModelConnection): ConnectionDraft {
  return {
    providerId: connection.providerId,
    model: connection.model,
    label: connection.label,
    reasoningEffort: connection.reasoningEffort ?? "",
    apiKey: "",
    baseUrl: connection.custom ? connection.baseUrl ?? "" : "",
    contextWindowTokens: connection.custom && connection.contextWindowTokens ? String(connection.contextWindowTokens) : "",
    maxOutputTokens: connection.custom && connection.maxOutputTokens ? String(connection.maxOutputTokens) : "",
    reasoningParameter: connection.custom ? connection.reasoningParameter ?? "none" : "none",
    structuredOutput: connection.custom ? connection.structuredOutput ?? "none" : "json_object",
  }
}

/** Reasoning tiers the draft may choose from, in the order the API knows them. */
export function offeredEfforts(provider: ModelProvider | null, draft: ConnectionDraft): ReasoningEffort[] {
  if (!provider) return []
  if (provider.kind === "catalog") return catalogModel(provider, draft.model)?.reasoning.efforts ?? []
  return provider.reasoningEfforts[draft.reasoningParameter] ?? []
}

/** The tier the provider uses when the draft leaves the effort unset, if the catalog knows it. */
export function defaultEffort(provider: ModelProvider | null, draft: ConnectionDraft): ReasoningEffort | null {
  return catalogModel(provider, draft.model)?.reasoning.default ?? null
}

/**
 * Whether saving needs the key typed again. A stored key is only ever sent to
 * the provider and address it was entered for (the API enforces the same rule).
 */
export function keyRequired(connection: ModelConnection | null, draft: ConnectionDraft): boolean {
  if (!connection) return true
  if (!connection.credentialConfigured) return true
  if (connection.providerId !== draft.providerId) return true
  return connection.custom && normalizedUrl(connection.baseUrl ?? "") !== normalizedUrl(draft.baseUrl)
}

export function isDirty(connection: ModelConnection | null, draft: ConnectionDraft, provider: ModelProvider | null): boolean {
  const baseline = connection ? draftFromConnection(connection) : draftForProvider(provider)
  return (Object.keys(draft) as Array<keyof ConnectionDraft>).some(key => draft[key] !== baseline[key])
}

/** What the API receives; fields a catalog provider takes from its catalog are left out. */
export function draftInput(provider: ModelProvider, draft: ConnectionDraft): ModelConnectionInput {
  const input: ModelConnectionInput = {
    providerId: provider.id,
    model: draft.model.trim(),
    label: draft.label.trim() || undefined,
    reasoningEffort: draft.reasoningEffort || undefined,
    apiKey: draft.apiKey.trim() || undefined,
  }
  if (provider.kind === "custom") {
    input.baseUrl = draft.baseUrl.trim()
    input.contextWindowTokens = positiveInteger(draft.contextWindowTokens) ?? undefined
    input.maxOutputTokens = positiveInteger(draft.maxOutputTokens) ?? undefined
    input.reasoningParameter = draft.reasoningParameter
    input.structuredOutput = draft.structuredOutput
  }
  return input
}

/** The context window a custom draft states, or null until it is a positive integer. */
export function draftWindow(draft: ConnectionDraft): number | null {
  return positiveInteger(draft.contextWindowTokens)
}

export function isCustom(provider: ModelProvider | null): provider is CustomProvider {
  return provider?.kind === "custom"
}

export function isCatalog(provider: ModelProvider | null): provider is CatalogProvider {
  return provider?.kind === "catalog"
}

function positiveInteger(value: string): number | null {
  const text = value.trim().replace(/[,，_\s]/g, "")
  if (!/^\d+$/.test(text)) return null
  const parsed = Number(text)
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null
}

function normalizedUrl(value: string): string {
  return value.trim().replace(/\/+$/, "")
}
