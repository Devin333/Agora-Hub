"use client"

import { useCallback, useEffect, useLayoutEffect, useState } from "react"
import { KeyRound, LoaderCircle, Pencil, Plus, ShieldCheck, Trash2 } from "lucide-react"
import { AccountServiceError, formatTokens } from "@/lib/account/client"
import {
  assignModelConnections,
  createModelConnection,
  deleteModelConnection,
  fetchModelConnections,
  updateModelConnection,
  verifyModelConnection,
  type ModelConnection,
  type ModelConnectionInput,
  type ModelConnectionStatus,
  type ModelConnectionsView,
  type ModelPurposeId,
} from "@/lib/account/model-connections"
import { notifyModelConnectionsChanged } from "@/lib/account/model-connection-events"
import { EFFORT_LABELS } from "./model-connection-draft"
import { ModelConnectionForm } from "./model-connection-form"
import { cx, formatTime, InlineError, LoadingLine, Notice, StatusBadge, type Tone } from "./settings-ui"
import styles from "../paper-agent-settings-panel.module.css"

type Editing = { kind: "new" } | { kind: "edit"; connectionId: string } | null
type Busy = { action: "load" | "save" | "assign" | "verify" | "delete"; connectionId?: string } | null

export const connectionStatusView: Record<ModelConnectionStatus, { label: string; tone: Tone }> = {
  verified: { label: "已验证可用", tone: "success" },
  unverified: { label: "已保存 · 未验证", tone: "warning" },
  key_missing: { label: "需要重新填写 Key", tone: "danger" },
  key_rotated: { label: "需要重新填写 Key", tone: "danger" },
  withdrawn: { label: "模型已下线", tone: "danger" },
}

/** What the settings summary card shows for the account's model connections. */
export type ModelConnectionsSummary = { label: string; tone: Tone }

export function summarizeModelConnections(view: ModelConnectionsView): ModelConnectionsSummary {
  const unassigned = view.purposes.filter(purpose => !view.assignments[purpose.id])
  const broken = view.purposes.filter(purpose => {
    const connection = view.connections.find(item => item.id === view.assignments[purpose.id])
    return connection && !["verified", "unverified"].includes(connection.status)
  })
  if (view.connections.length === 0) return { label: "未配置", tone: "neutral" }
  if (broken.length) return { label: `${broken.length} 个用途需要处理`, tone: "danger" }
  if (unassigned.length) return { label: `${unassigned.length} 个用途未分配`, tone: "warning" }
  const allVerified = view.purposes.every(purpose =>
    view.connections.find(item => item.id === view.assignments[purpose.id])?.status === "verified",
  )
  return allVerified ? { label: "全部用途可用", tone: "success" } : { label: "已分配 · 未全部验证", tone: "warning" }
}

/** The account's model connections and which one serves each purpose. */
export function ModelConnectionsSection({
  onDraftChange,
  onSummaryChange,
}: {
  onDraftChange: (dirty: boolean) => void
  onSummaryChange: (summary: ModelConnectionsSummary | null) => void
}) {
  const [view, setView] = useState<ModelConnectionsView | null>(null)
  const [editing, setEditing] = useState<Editing>(null)
  const [formDirty, setFormDirty] = useState(false)
  const [busy, setBusy] = useState<Busy>({ action: "load" })
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")

  const adopt = useCallback((next: ModelConnectionsView) => {
    setView(next)
    onSummaryChange(summarizeModelConnections(next))
  }, [onSummaryChange])

  const reload = useCallback(async (signal?: AbortSignal) => {
    try {
      adopt(await fetchModelConnections(signal))
    } catch (cause) {
      if (signal?.aborted) return
      setError(messageOf(cause, "无法读取模型连接。"))
      onSummaryChange(null)
    }
  }, [adopt, onSummaryChange])

  useEffect(() => {
    const controller = new AbortController()
    void reload(controller.signal).finally(() => { if (!controller.signal.aborted) setBusy(null) })
    return () => controller.abort()
  }, [reload])

  // Layout effect: the dialog's close guard learns about the draft before it is painted.
  useLayoutEffect(() => { onDraftChange(editing !== null && formDirty) }, [editing, formDirty, onDraftChange])

  async function run(next: NonNullable<Busy>, action: () => Promise<ModelConnectionsView>, success: string): Promise<boolean> {
    if (busy) return false
    setBusy(next); setError(""); setNotice("")
    try {
      adopt(await action())
      notifyModelConnectionsChanged()
      setNotice(success)
      return true
    } catch (cause) {
      setError(messageOf(cause, "操作未完成，请稍后重试。"))
      if (cause instanceof AccountServiceError && cause.code === "model_connection_conflict") {
        // Another tab changed the settings: show the current state, keep nothing stale.
        await reload()
      }
      return false
    } finally {
      setBusy(null)
    }
  }

  function closeForm() { setEditing(null); setFormDirty(false) }

  async function submit(input: ModelConnectionInput) {
    if (!view || !editing) return
    const saved = editing.kind === "new"
      ? await run({ action: "save" }, () => createModelConnection(input, view.revision), "模型连接已保存。建议点击“验证”确认可用。")
      : await run({ action: "save", connectionId: editing.connectionId }, () => updateModelConnection(editing.connectionId, input, view.revision), input.apiKey ? "API Key 已更新。建议点击“验证”确认可用。" : "模型连接已更新。")
    if (saved) closeForm()
  }

  function assign(purpose: ModelPurposeId, connectionId: string) {
    if (!view) return
    void run({ action: "assign" }, () => assignModelConnections({ [purpose]: connectionId || null }, view.revision), connectionId ? "用途分配已保存。" : "已取消这个用途的分配。")
  }

  function remove(connection: ModelConnection) {
    if (!view) return
    const purposes = view.purposes.filter(purpose => connection.purposes.includes(purpose.id)).map(purpose => purpose.label)
    const consequence = purposes.length ? `“${purposes.join("”“")}”将没有可用的模型，直到重新分配。` : "它目前没有分配给任何用途。"
    if (!window.confirm(`删除模型连接“${connection.label}”？${consequence}`)) return
    void run({ action: "delete", connectionId: connection.id }, () => deleteModelConnection(connection.id, view.revision), "模型连接已删除。")
  }

  if (busy?.action === "load") return <LoadingLine />
  if (!view) return <InlineError>{error || "无法读取模型连接。"}</InlineError>

  const editedConnection = editing?.kind === "edit" ? view.connections.find(item => item.id === editing.connectionId) ?? null : null
  return <div className={styles.configEditor}>
    <PurposeAssignments view={view} busy={busy !== null} onAssign={assign} />
    {view.connections.length === 0 && editing === null && <Notice icon={<KeyRound size={17} />} title="还没有模型连接">
      添加一个模型连接（服务商、模型和你的 API Key），再把它分配给需要的用途。一个窗口足够大的模型可以同时服务所有用途。
    </Notice>}
    {view.connections.length > 0 && <ul className={styles.accountList} aria-label="模型连接">
      {view.connections.map(connection => <ConnectionRow
        key={connection.id}
        view={view}
        connection={connection}
        busy={busy}
        editing={editing !== null}
        onEdit={() => { setEditing({ kind: "edit", connectionId: connection.id }); setFormDirty(false); setError(""); setNotice("") }}
        onVerify={() => void run({ action: "verify", connectionId: connection.id }, () => verifyModelConnection(connection.id), "验证通过，模型服务可以正常使用这个 Key。")}
        onDelete={() => remove(connection)}
      />)}
    </ul>}
    {editing
      ? <ModelConnectionForm
        key={editing.kind === "edit" ? editing.connectionId : "new"}
        view={view}
        connection={editedConnection}
        busy={busy?.action === "save"}
        onSubmit={input => void submit(input)}
        onCancel={closeForm}
        onDraftChange={setFormDirty}
      />
      : <div className={styles.configActions}>
        <button type="button" className={styles.primaryButton} disabled={busy !== null} onClick={() => { setEditing({ kind: "new" }); setFormDirty(false); setError(""); setNotice("") }}>
          <Plus size={16} />添加模型连接
        </button>
      </div>}
    <p className={styles.footnote}>“验证”会用你的 Key 读取一次模型列表；不提供模型列表的自定义地址会改为发送一次极小的请求，可能产生少量费用。</p>
    {error && <InlineError>{error}</InlineError>}
    {notice && <p className={cx(styles.resultLine, styles.success)} role="status">{notice}</p>}
  </div>
}

function PurposeAssignments({ view, busy, onAssign }: { view: ModelConnectionsView; busy: boolean; onAssign: (purpose: ModelPurposeId, connectionId: string) => void }) {
  return <fieldset className={styles.configFieldset} disabled={busy}>
    <legend>用途分配</legend>
    <div className={styles.formGrid}>
      {view.purposes.map(purpose => {
        const assigned = view.assignments[purpose.id] ?? ""
        return <label key={purpose.id}><span>{purpose.label}</span>
          <select value={assigned} onChange={event => onAssign(purpose.id, event.target.value)}>
            <option value="">未分配</option>
            {view.connections.map(connection => {
              const fit = connection.fits[purpose.id]
              // A connection that cannot hold the purpose stays visible, so the
              // user sees why it is not offered rather than wondering where it went.
              return <option key={connection.id} value={connection.id} disabled={!fit?.fits && connection.id !== assigned}>
                {connection.label}{fit?.fits ? "" : `（${unfitReason(fit?.reason ?? null, fit?.requiredWindow ?? null)}）`}
              </option>
            })}
          </select>
        </label>
      })}
    </div>
    <p className={styles.footnote}>每个用途对模型窗口的要求不同；不满足要求的连接会显示原因，不能选择。</p>
  </fieldset>
}

function ConnectionRow({
  view,
  connection,
  busy,
  editing,
  onEdit,
  onVerify,
  onDelete,
}: {
  view: ModelConnectionsView
  connection: ModelConnection
  busy: Busy
  editing: boolean
  onEdit: () => void
  onVerify: () => void
  onDelete: () => void
}) {
  const status = connectionStatusView[connection.status]
  const purposes = view.purposes.filter(purpose => connection.purposes.includes(purpose.id)).map(purpose => purpose.label)
  const verifying = busy?.action === "verify" && busy.connectionId === connection.id
  const deleting = busy?.action === "delete" && busy.connectionId === connection.id
  const keyUsable = connection.status === "verified" || connection.status === "unverified"
  return <li>
    <div className={styles.accountMain}>
      <strong>{connection.label}</strong>
      <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
      <span className={styles.requirement}>{purposes.length ? purposes.join("、") : "未分配用途"}</span>
    </div>
    <p>
      {connection.providerLabel} · {connection.modelLabel}
      {connection.contextWindowTokens ? ` · 窗口 ${formatTokens(connection.contextWindowTokens)}` : ""}
      {connection.reasoningEffort ? ` · 思考 ${EFFORT_LABELS[connection.reasoningEffort]}` : ""}
      {connection.keyHint ? ` · Key 尾号 ${connection.keyHint}` : ""}
    </p>
    {connection.custom && connection.baseUrl && <p>地址：<code>{connection.baseUrl}</code></p>}
    {connection.verifiedAt && <p>最近验证：{formatTime(connection.verifiedAt)}</p>}
    {!keyUsable && <p className={styles.draftWarning}>{statusHint(connection.status)}</p>}
    <div className={styles.configActions}>
      <button type="button" className={styles.secondaryButton} disabled={busy !== null || editing} onClick={onEdit}><Pencil size={16} />{keyUsable ? "编辑" : "重新填写"}</button>
      <button type="button" className={styles.secondaryButton} disabled={busy !== null || editing || !keyUsable} onClick={onVerify}>{verifying ? <LoaderCircle className={styles.spinner} size={16} /> : <ShieldCheck size={16} />}验证</button>
      <button type="button" className={styles.secondaryButton} disabled={busy !== null || editing} onClick={onDelete}>{deleting ? <LoaderCircle className={styles.spinner} size={16} /> : <Trash2 size={16} />}删除</button>
    </div>
  </li>
}

function statusHint(status: ModelConnectionStatus): string {
  if (status === "key_rotated") return "服务器的加密密钥已更换，原来保存的 Key 无法再解开，请重新填写一次。"
  if (status === "key_missing") return "这个连接的 Key 需要重新填写，之后即可正常使用。"
  if (status === "withdrawn") return "管理员已下线这个服务商或模型，请编辑并改用其他模型。"
  return ""
}

function unfitReason(reason: string | null, requiredWindow: number | null): string {
  if (reason === "model_connection_structured_output_required") return "需要 JSON 输出"
  if (reason === "model_connection_window_too_small") return `需要 ${formatTokens(requiredWindow) ?? "更大"} 窗口`
  if (reason === "model_connection_withdrawn") return "模型已下线"
  return "不满足要求"
}

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof AccountServiceError ? cause.message : fallback
}
