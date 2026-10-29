"use client"

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import { KeyRound, LoaderCircle, ShieldCheck, Trash2 } from "lucide-react"
import {
  AccountServiceError,
  deleteModelCredential,
  fetchModelCredential,
  saveModelCredential,
  verifyModelCredential,
  type ModelCatalog,
  type ModelCredential,
  type ModelCredentialPayload,
  type ModelCredentialStatus,
} from "@/lib/account/client"
import { notifyModelCredentialChanged } from "@/lib/account/model-credential-events"
import { cx, formatTime, InlineError, LoadingLine, Notice, StatusBadge, type Tone } from "./settings-ui"
import styles from "../paper-agent-settings-panel.module.css"

type Busy = "load" | "save" | "verify" | "delete" | null
type Draft = { providerId: string; model: string; apiKey: string }

export const credentialStatusView: Record<ModelCredentialStatus, { label: string; tone: Tone }> = {
  not_configured: { label: "未配置", tone: "neutral" },
  key_rotated: { label: "需要重新填写", tone: "danger" },
  unverified: { label: "已保存 · 未验证", tone: "warning" },
  verified: { label: "已验证可用", tone: "success" },
}

/** The account's own model API key: write-only, stored encrypted on the server. */
export function ModelCredentialSection({
  onDraftChange,
  onStatusChange,
}: {
  onDraftChange: (dirty: boolean) => void
  onStatusChange: (status: ModelCredentialStatus | null) => void
}) {
  const [payload, setPayload] = useState<ModelCredentialPayload | null>(null)
  const [draft, setDraft] = useState<Draft>({ providerId: "", model: "", apiKey: "" })
  const [busy, setBusy] = useState<Busy>("load")
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  const keyInputRef = useRef<HTMLInputElement>(null)

  const adopt = useCallback((next: ModelCredentialPayload) => {
    setPayload(next)
    setDraft(initialDraft(next.credential, next.catalog))
    onStatusChange(next.credential.status)
  }, [onStatusChange])

  useEffect(() => {
    const controller = new AbortController()
    fetchModelCredential(controller.signal)
      .then(adopt)
      .catch(cause => {
        if (controller.signal.aborted) return
        setError(messageOf(cause, "无法读取模型 API 配置。"))
        onStatusChange(null)
      })
      .finally(() => { if (!controller.signal.aborted) setBusy(null) })
    return () => controller.abort()
  }, [adopt, onStatusChange])

  const credential = payload?.credential ?? null
  const catalog = payload?.catalog ?? null
  const provider = catalog?.providers.find(item => item.id === draft.providerId) ?? null
  const providerChanged = Boolean(credential?.configured && credential.providerId !== draft.providerId)
  // A new provider, a rotated server key or a first save all need the key itself.
  const keyRequired = !credential?.configured || providerChanged || credential.status === "key_rotated"
  const dirty = useMemo(() => {
    if (!credential || !catalog) return false
    const baseline = initialDraft(credential, catalog)
    return draft.apiKey.length > 0 || draft.providerId !== baseline.providerId || draft.model !== baseline.model
  }, [catalog, credential, draft])

  // Layout effect: the dialog's close guard learns about the draft before it is painted.
  useLayoutEffect(() => { onDraftChange(dirty) }, [dirty, onDraftChange])

  function update(patch: Partial<Draft>) {
    setDraft(previous => ({ ...previous, ...patch }))
    setError(""); setNotice("")
  }

  function chooseProvider(providerId: string) {
    const next = catalog?.providers.find(item => item.id === providerId)
    update({ providerId, model: next?.defaultModel ?? "" })
  }

  async function run(kind: Exclude<Busy, "load" | null>, action: () => Promise<ModelCredentialPayload>, success: string) {
    if (busy) return
    setBusy(kind); setError(""); setNotice("")
    try {
      adopt(await action())
      notifyModelCredentialChanged()
      setNotice(success)
    } catch (cause) {
      setError(messageOf(cause, "操作未完成，请稍后重试。"))
      if (cause instanceof AccountServiceError && cause.code === "model_credential_conflict") {
        // Another tab changed the stored key: show the current state, keep nothing stale.
        await fetchModelCredential().then(adopt).catch(() => undefined)
      }
    } finally {
      setBusy(null)
    }
  }

  function save() {
    if (!provider || !draft.model) { setError("请选择模型服务商和模型。"); return }
    const apiKey = draft.apiKey.trim()
    if (keyRequired && !apiKey) { setError("请填写 API Key。"); keyInputRef.current?.focus(); return }
    void run("save", () => saveModelCredential({
      providerId: provider.id,
      model: draft.model,
      apiKey: apiKey || undefined,
      expectedRevision: credential?.configured ? credential.revision : null,
    }), apiKey ? "API Key 已加密保存。建议点击“验证”确认可用。" : "模型已更新。")
  }

  function remove() {
    if (!window.confirm("删除已保存的模型 API Key？删除后，需要模型的研究功能将不可用，直到重新填写。")) return
    void run("delete", deleteModelCredential, "API Key 已删除。")
  }

  if (busy === "load") return <LoadingLine />
  if (!credential || !catalog) return <InlineError>{error || "无法读取模型 API 配置。"}</InlineError>

  const status = credentialStatusView[credential.status]
  return <div className={styles.configEditor}>
    <div className={styles.configStatus}>
      <div>
        <strong>当前状态</strong>
        <span>{credential.configured
          ? `${providerLabel(catalog, credential.providerId)} · ${credential.model ?? "未知模型"} · Key 尾号 ${credential.keyHint ?? "****"}`
          : "尚未填写。需要模型的分析、问答等功能会提示你先配置。"}</span>
        {credential.verifiedAt && <small>最近验证：{formatTime(credential.verifiedAt)}</small>}
      </div>
      <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
    </div>
    {credential.status === "key_rotated" && <Notice warning icon={<KeyRound size={17} />} title="需要重新填写 API Key">服务器的加密密钥已更换，原来保存的 Key 无法再解开。请重新粘贴一次，之后即可正常使用。</Notice>}
    <fieldset className={styles.configFieldset} disabled={busy !== null}>
      <legend>模型服务</legend>
      <div className={styles.formGrid}>
        <label><span>服务商</span>
          <select value={draft.providerId} onChange={event => chooseProvider(event.target.value)}>
            {catalog.providers.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
        </label>
        <label><span>模型</span>
          <select value={draft.model} onChange={event => update({ model: event.target.value })}>
            {(provider?.models ?? []).map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
        </label>
        <label className={styles.wideField}><span>API Key</span>
          <input
            ref={keyInputRef}
            type="password"
            value={draft.apiKey}
            onChange={event => update({ apiKey: event.target.value })}
            placeholder={keyRequired ? "粘贴你的 API Key" : `已保存（尾号 ${credential.keyHint ?? "****"}），留空则保持不变`}
            autoComplete="off"
            spellCheck={false}
            maxLength={512}
            aria-describedby="model-credential-key-help"
          />
        </label>
      </div>
      <p id="model-credential-key-help" className={styles.footnote}>
        {provider ? <>Key 只会发送到 <code>{provider.baseUrl}</code>。</> : null}
        保存后在服务器上加密存储，页面不会再显示完整 Key；只有你的账号会使用它。
      </p>
    </fieldset>
    <div className={styles.configActions}>
      {credential.configured && <button type="button" className={styles.secondaryButton} disabled={busy !== null} onClick={remove}>{busy === "delete" ? <LoaderCircle className={styles.spinner} size={16} /> : <Trash2 size={16} />}删除</button>}
      {credential.configured && <button type="button" className={styles.secondaryButton} disabled={busy !== null || dirty || credential.status === "key_rotated"} title={dirty ? "请先保存修改再验证" : undefined} onClick={() => void run("verify", verifyModelCredential, "验证通过，模型服务可以正常使用这个 Key。")}>{busy === "verify" ? <LoaderCircle className={styles.spinner} size={16} /> : <ShieldCheck size={16} />}验证</button>}
      <button type="button" className={styles.primaryButton} disabled={busy !== null || !dirty} onClick={save}>{busy === "save" ? <LoaderCircle className={styles.spinner} size={16} /> : <KeyRound size={16} />}保存</button>
    </div>
    <p className={styles.footnote}>“验证”会用你的 Key 向模型服务发送一次极小的请求，可能产生少量费用。</p>
    {error && <InlineError>{error}</InlineError>}
    {notice && <p className={cx(styles.resultLine, styles.success)} role="status">{notice}</p>}
  </div>
}

function initialDraft(credential: ModelCredential, catalog: ModelCatalog): Draft {
  const stored = credential.configured ? catalog.providers.find(item => item.id === credential.providerId) : undefined
  const provider = stored ?? catalog.providers[0]
  if (!provider) return { providerId: "", model: "", apiKey: "" }
  const model = stored && credential.model && provider.models.some(item => item.id === credential.model) ? credential.model : provider.defaultModel
  return { providerId: provider.id, model, apiKey: "" }
}

function providerLabel(catalog: ModelCatalog, providerId: string | null): string {
  return catalog.providers.find(item => item.id === providerId)?.label ?? providerId ?? "未知服务商"
}

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof AccountServiceError ? cause.message : fallback
}
