"use client"

import { useId, useRef, useState } from "react"
import { KeyRound, ListRestart, LoaderCircle, X } from "lucide-react"
import { AccountServiceError, formatTokens } from "@/lib/account/client"
import {
  discoverModels,
  type ModelConnection,
  type ModelConnectionInput,
  type ModelConnectionsView,
  type ReasoningEffort,
} from "@/lib/account/model-connections"
import {
  catalogModel,
  defaultEffort,
  draftFit,
  draftForProvider,
  draftFromConnection,
  draftInput,
  draftLimits,
  EFFORT_LABELS,
  isCustom,
  isDirty,
  keyRequired,
  offeredEfforts,
  providerById,
  REASONING_PARAMETER_LABELS,
  STRUCTURED_OUTPUT_LABELS,
  type ConnectionDraft,
} from "./model-connection-draft"
import { cx, InlineError } from "./settings-ui"
import styles from "../paper-agent-settings-panel.module.css"

/** Create or edit one connection; the key is entered here and never shown again. */
export function ModelConnectionForm({
  view,
  connection,
  busy,
  onSubmit,
  onCancel,
  onDraftChange,
}: {
  view: ModelConnectionsView
  connection: ModelConnection | null
  busy: boolean
  onSubmit: (input: ModelConnectionInput) => void
  onCancel: () => void
  onDraftChange: (dirty: boolean) => void
}) {
  const formId = useId()
  const [draft, setDraft] = useState<ConnectionDraft>(() =>
    connection ? draftFromConnection(connection) : draftForProvider(view.catalog.providers[0] ?? null),
  )
  const [error, setError] = useState("")
  const [discovered, setDiscovered] = useState<string[] | null>(null)
  const [discovering, setDiscovering] = useState(false)
  const keyInputRef = useRef<HTMLInputElement>(null)

  const provider = providerById(view, draft.providerId)
  const custom = isCustom(provider)
  const needsKey = keyRequired(connection, draft)
  const efforts = offeredEfforts(provider, draft)
  const providerDefault = defaultEffort(provider, draft)
  const limits = draftLimits(provider, draft)

  function update(patch: Partial<ConnectionDraft>) {
    setDraft(previous => {
      const next = { ...previous, ...patch }
      onDraftChange(isDirty(connection, next, providerById(view, next.providerId)))
      return next
    })
    setError("")
  }

  function chooseProvider(providerId: string) {
    const next = providerById(view, providerId)
    setDiscovered(null)
    // Keep the name and key typed so far; everything provider-specific restarts.
    update({ ...draftForProvider(next), label: draft.label, apiKey: draft.apiKey })
  }

  function chooseModel(model: string) {
    const nextEfforts = offeredEfforts(provider, { ...draft, model })
    // A tier the newly chosen model does not offer would be refused on save.
    update({ model, reasoningEffort: nextEfforts.includes(draft.reasoningEffort as ReasoningEffort) ? draft.reasoningEffort : "" })
  }

  function chooseReasoningParameter(reasoningParameter: string) {
    const nextEfforts = custom ? provider.reasoningEfforts[reasoningParameter] ?? [] : []
    update({ reasoningParameter, reasoningEffort: nextEfforts.includes(draft.reasoningEffort as ReasoningEffort) ? draft.reasoningEffort : "" })
  }

  async function listModels() {
    if (!custom) return
    const apiKey = draft.apiKey.trim()
    const reuseStored = !apiKey && connection !== null && !needsKey
    if (!apiKey && !reuseStored) { setError("请先填写 API Key，再获取模型列表。"); keyInputRef.current?.focus(); return }
    setDiscovering(true); setError("")
    try {
      const models = await discoverModels({
        providerId: provider.id,
        baseUrl: draft.baseUrl.trim(),
        apiKey: apiKey || undefined,
        connectionId: reuseStored ? connection.id : undefined,
      })
      setDiscovered(models)
      if (models.length === 0) setError("这个地址没有返回任何模型，请手动填写模型 ID。")
    } catch (cause) {
      setError(cause instanceof AccountServiceError ? cause.message : "获取模型列表失败，请稍后重试。")
    } finally {
      setDiscovering(false)
    }
  }

  function submit() {
    if (!provider) { setError("请选择模型服务商。"); return }
    if (!draft.model.trim()) { setError(custom ? "请填写模型 ID。" : "请选择模型。"); return }
    if (custom && !draft.baseUrl.trim()) { setError("请填写服务地址。"); return }
    if (custom && limits.contextWindowTokens === null) { setError("请填写上下文窗口大小（正整数）。"); return }
    if (needsKey && !draft.apiKey.trim()) { setError("请填写 API Key。"); keyInputRef.current?.focus(); return }
    onSubmit(draftInput(provider, draft))
  }

  return <form
    className={styles.configEditor}
    aria-labelledby={`${formId}-title`}
    onSubmit={event => { event.preventDefault(); submit() }}
  >
    <div className={styles.configStatus}>
      <strong id={`${formId}-title`}>{connection ? `编辑“${connection.label}”` : "添加模型连接"}</strong>
      <button type="button" className={styles.iconButton} aria-label="取消编辑" disabled={busy} onClick={onCancel}><X size={16} /></button>
    </div>
    <fieldset className={styles.configFieldset} disabled={busy}>
      <legend>模型</legend>
      <div className={styles.formGrid}>
        <label><span>服务商</span>
          <select value={draft.providerId} onChange={event => chooseProvider(event.target.value)}>
            {view.catalog.providers.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
        </label>
        {custom
          ? <label><span>模型 ID</span>
            <input
              value={draft.model}
              onChange={event => update({ model: event.target.value })}
              list={discovered ? `${formId}-models` : undefined}
              placeholder="例如 qwen3-max"
              autoComplete="off"
              spellCheck={false}
              maxLength={128}
            />
            {discovered && <datalist id={`${formId}-models`}>{discovered.map(item => <option key={item} value={item} />)}</datalist>}
          </label>
          : <label><span>模型</span>
            <select value={draft.model} onChange={event => chooseModel(event.target.value)}>
              {(provider?.kind === "catalog" ? provider.models : []).map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
            </select>
          </label>}
        {custom && <label className={styles.wideField}><span>服务地址（Base URL）</span>
          <input
            value={draft.baseUrl}
            onChange={event => { setDiscovered(null); update({ baseUrl: event.target.value }) }}
            placeholder={provider.protocol === "anthropic_messages" ? "https://api.example.com" : "https://api.example.com/v1"}
            autoComplete="off"
            spellCheck={false}
            inputMode="url"
            maxLength={2048}
          />
        </label>}
        {custom && <label><span>上下文窗口（token）</span>
          <input value={draft.contextWindowTokens} onChange={event => update({ contextWindowTokens: event.target.value })} inputMode="numeric" placeholder="例如 128000" />
        </label>}
        {custom && <label><span>最大输出（token，可选）</span>
          <input value={draft.maxOutputTokens} onChange={event => update({ maxOutputTokens: event.target.value })} inputMode="numeric" placeholder="不填则不限制" />
        </label>}
        {custom && <label><span>思考参数格式</span>
          <select value={draft.reasoningParameter} onChange={event => chooseReasoningParameter(event.target.value)}>
            {provider.reasoningParameters.map(item => <option key={item} value={item}>{REASONING_PARAMETER_LABELS[item] ?? item}</option>)}
          </select>
        </label>}
        {custom && <label><span>结构化输出</span>
          <select value={draft.structuredOutput} onChange={event => update({ structuredOutput: event.target.value })}>
            {provider.structuredOutputModes.map(item => <option key={item} value={item}>{STRUCTURED_OUTPUT_LABELS[item] ?? item}</option>)}
          </select>
        </label>}
        <label><span>思考强度</span>
          <select value={draft.reasoningEffort} disabled={efforts.length === 0} onChange={event => update({ reasoningEffort: event.target.value as ReasoningEffort | "" })}>
            <option value="">{providerDefault ? `服务商默认（${EFFORT_LABELS[providerDefault]}）` : "服务商默认"}</option>
            {efforts.map(item => <option key={item} value={item}>{EFFORT_LABELS[item]}</option>)}
          </select>
        </label>
        <label><span>名称（可选）</span>
          <input value={draft.label} onChange={event => update({ label: event.target.value })} placeholder={defaultLabel(view, draft)} maxLength={64} />
        </label>
        <label className={styles.wideField}><span>API Key</span>
          <input
            ref={keyInputRef}
            type="password"
            value={draft.apiKey}
            onChange={event => update({ apiKey: event.target.value })}
            placeholder={needsKey ? "粘贴你的 API Key" : `已保存（尾号 ${connection?.keyHint ?? "****"}），留空则保持不变`}
            autoComplete="off"
            spellCheck={false}
            maxLength={512}
            aria-describedby={`${formId}-key-help`}
          />
        </label>
      </div>
      <p id={`${formId}-key-help`} className={styles.footnote}>
        {provider && !custom ? <>Key 只会发送到 <code>{provider.baseUrl}</code>。</> : null}
        {custom ? "Key 只会发送到你填写的地址；地址必须是公网 HTTPS，内网地址需要管理员加入白名单。更换地址后需要重新填写 Key。" : null}
        保存后在服务器上加密存储，页面不会再显示完整 Key。
      </p>
      {custom && <div className={styles.configActions}>
        <button type="button" className={styles.secondaryButton} disabled={busy || discovering || !draft.baseUrl.trim()} onClick={() => void listModels()}>
          {discovering ? <LoaderCircle className={styles.spinner} size={16} /> : <ListRestart size={16} />}获取模型列表
        </button>
      </div>}
    </fieldset>
    <PurposeFitPreview view={view} limits={limits} />
    {error && <InlineError>{error}</InlineError>}
    <div className={styles.configActions}>
      <button type="button" className={styles.secondaryButton} disabled={busy} onClick={onCancel}>取消</button>
      <button type="submit" className={styles.primaryButton} disabled={busy}>
        {busy ? <LoaderCircle className={styles.spinner} size={16} /> : <KeyRound size={16} />}保存
      </button>
    </div>
  </form>
}

function PurposeFitPreview({ view, limits }: { view: ModelConnectionsView; limits: ReturnType<typeof draftLimits> }) {
  return <ul className={styles.capabilityList} aria-label="可用于哪些用途">
    {view.purposes.map(purpose => {
      const fit = draftFit(view, purpose, limits)
      return <li key={purpose.id}>
        <div className={styles.capabilityMain}>
          <span className={styles.capabilityName}>{purpose.label}</span>
          <span className={cx(styles.badge, fit.fits ? styles.badgeSuccess : fit.reason === "unknown" ? styles.badgeNeutral : styles.badgeWarning)}>
            {fit.fits ? "可用" : fit.reason === "unknown" ? "待填写窗口" : "不可用"}
          </span>
        </div>
        <p>{fitText(fit, limits.contextWindowTokens)}</p>
      </li>
    })}
  </ul>
}

function fitText(fit: ReturnType<typeof draftFit>, window: number | null): string {
  if (fit.reason === "structured_output") return "需要能返回 JSON 对象的模型。"
  if (fit.reason === "unknown") return "填写上下文窗口后可以判断。"
  const required = formatTokens(fit.requiredWindow) ?? "—"
  if (fit.fits) return `需要 ${required} 窗口，这个模型有 ${formatTokens(window) ?? "—"}。`
  return `需要至少 ${required} 窗口，这个模型只有 ${formatTokens(window) ?? "—"}。`
}

function defaultLabel(view: ModelConnectionsView, draft: ConnectionDraft): string {
  const provider = providerById(view, draft.providerId)
  const model = catalogModel(provider, draft.model)?.label ?? (draft.model.trim() || "模型")
  return provider ? `${provider.label} · ${model}` : "连接名称"
}
