"use client"

import { useEffect, useRef, useState } from "react"
import { AlertTriangle, CheckCircle2, LoaderCircle, RefreshCw, ShieldAlert, Trash2, X } from "lucide-react"
import {
  applyDesktopDataClear,
  DATA_CLEAR_CONFIRMATION_TEXT,
  hasDesktopDataClearBridge,
  previewDesktopDataClear,
  readDesktopDataClearStatus,
  type DesktopDataClearApplyResult,
  type DesktopDataClearPreviewResult,
  type DesktopDataClearStatusResult,
} from "./paper-agent-data-clear-runtime"
import styles from "./paper-agent-settings-panel.module.css"

type BusyAction = "preview" | "apply" | "status" | null

type LifecycleBlock = {
  blocked: boolean
  message: string | null
}

const stoppedPreviewCancellationNotice = "预览已安全停止本机服务；取消后可正常关闭并重新打开 App 恢复服务，不会重放业务请求。取消不会自动恢复服务。"

export function PaperAgentPreAuthDataClearModal({
  open,
  overlayClassName,
  hasProtectedDrafts,
  activeTaskCount,
  onRequestClose,
}: {
  open: boolean
  overlayClassName?: string
  hasProtectedDrafts: boolean
  activeTaskCount: number
  onRequestClose: () => void
}) {
  const dialogRef = useRef<HTMLElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault()
        event.stopImmediatePropagation()
        onRequestClose()
        return
      }
      if (event.key !== "Tab") return
      const dialog = dialogRef.current
      if (!dialog) return
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>('button, input:not(:disabled), [href], [tabindex]:not([tabindex="-1"])'))
        .filter(element => !element.matches(":disabled"))
      if (!focusable.length) return
      const current = focusable.indexOf(document.activeElement as HTMLElement)
      const next = current < 0
        ? (event.shiftKey ? focusable.length - 1 : 0)
        : (current + (event.shiftKey ? -1 : 1) + focusable.length) % focusable.length
      event.preventDefault()
      focusable[next]?.focus()
    }
    document.addEventListener("keydown", onKeyDown, true)
    closeRef.current?.focus({ preventScroll: true })
    return () => {
      document.removeEventListener("keydown", onKeyDown, true)
      if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true })
    }
  }, [onRequestClose, open])

  if (!open) return null
  return <div className={overlayClassName} role="presentation" onMouseDown={event => {
    if (event.target === event.currentTarget) onRequestClose()
  }}>
    <section ref={dialogRef} className={styles.panel} role="dialog" aria-modal="true" aria-labelledby="preauth-data-clear-title">
      <header className={styles.header}>
        <div><p className={styles.eyebrow}>本机恢复入口</p><h2 id="preauth-data-clear-title">管理/清除本机数据</h2><p>无需登录即可管理当前操作系统用户所属的 Agora 资料目录；此窗口不会读取账号论文库、笔记或个人记忆内容。</p></div>
        <button ref={closeRef} type="button" className={styles.iconButton} aria-label="关闭本机数据管理" onClick={onRequestClose}><X size={18} /></button>
      </header>
      <div className={styles.sections}>
        <section className={`${styles.card} ${styles.clearCard}`}>
          <PaperAgentDataClearPanel hasProtectedDrafts={hasProtectedDrafts} activeTaskCount={activeTaskCount} />
        </section>
      </div>
    </section>
  </div>
}
export function PaperAgentDataClearPanel({
  hasProtectedDrafts,
  activeTaskCount
}: {
  hasProtectedDrafts: boolean
  activeTaskCount?: number
}) {
  const available = hasDesktopDataClearBridge()
  const [preview, setPreview] = useState<DesktopDataClearPreviewResult | null>(null)
  const [confirmationText, setConfirmationText] = useState("")
  const [applyResult, setApplyResult] = useState<DesktopDataClearApplyResult | null>(null)
  const [status, setStatus] = useState<DesktopDataClearStatusResult | null>(null)
  const [busy, setBusy] = useState<BusyAction>(null)
  const [callError, setCallError] = useState<string | null>(null)
  const [previewInvalidated, setPreviewInvalidated] = useState(false)
  const [needsStatusRefresh, setNeedsStatusRefresh] = useState(true)
  const [clock, setClock] = useState(() => Date.now())
  const lifecycle = lifecycleBlock(hasProtectedDrafts, activeTaskCount)
  const expiresAt = preview?.ok && preview.expiresAt ? Date.parse(preview.expiresAt) : Number.NaN
  const expired = preview?.ok === true && (!Number.isFinite(expiresAt) || expiresAt <= clock)
  const prepared = status?.state === "PREPARED" || status?.state === "FINALIZING"
  const phraseMatches = confirmationText === DATA_CLEAR_CONFIRMATION_TEXT
  const showStoppedServiceNotice = preview?.ok === true && preview.stopped === true
    && status?.ok === true && ["IDLE", "NONE", "PREVIEW"].includes(status.state)
    && !needsStatusRefresh


  useEffect(() => {
    if (!available) return
    let active = true
    setBusy("status")
    readDesktopDataClearStatus().then(result => {
      if (active) {
        setStatus(result)
        setNeedsStatusRefresh(dataClearStatusNeedsRefresh(result))
      }
    }).catch(() => {
      if (active) setCallError("无法读取清除维护状态；未把未知状态当作可清除或已清除。")
    }).finally(() => {
      if (active) setBusy(current => current === "status" ? null : current)
    })
    return () => { active = false }
  }, [available])

  useEffect(() => {
    if (!preview?.expiresAt || expired) return
    const delay = Math.max(0, Date.parse(preview.expiresAt) - Date.now())
    if (!Number.isFinite(delay)) return
    const timer = window.setTimeout(() => setClock(Date.now()), Math.min(delay + 25, 2_147_483_647))
    return () => window.clearTimeout(timer)
  }, [expired, preview?.expiresAt])

  const canApply = available
    && preview?.ok === true
    && !expired
    && !previewInvalidated
    && !needsStatusRefresh
    && phraseMatches
    && !lifecycle.blocked
    && busy === null
    && !prepared

  async function runPreview() {
    if (!available || lifecycle.blocked || busy || prepared || needsStatusRefresh) return
    setBusy("preview")
    setCallError(null)
    try {
      const result = await previewDesktopDataClear()
      if (result.ok) {
        setPreview(result)
        setConfirmationText("")
        setApplyResult(null)
        setPreviewInvalidated(false)
        setClock(Date.now())
      } else {
        setApplyResult(null)
        setCallError(result.cancelled
          ? "预览已取消；本次预览未执行删除。"
          : `预览未完成；尚未授权本次删除。结果码：${result.code}`)
      }
    } catch {
      setCallError("清除预览响应无法安全验证；尚未授权本次删除。")
    } finally {
      setBusy(null)
    }
  }

  async function runApply() {
    if (!canApply || !preview?.previewId) return
    setBusy("apply")
    setApplyResult(null)
    setCallError(null)
    try {
      const result = await applyDesktopDataClear(preview.previewId, confirmationText)
      setApplyResult(result)
      const cancelled = isConfirmedDataClearCancellation(result)
      const rejectedBeforeAuthorization = isDataClearRejectedBeforeAuthorization(result)
      if (!cancelled && !rejectedBeforeAuthorization) {
        setStatus({ ok: result.ok, state: result.state, code: result.code, message: result.message, operationId: result.operationId })
        if (!result.ok) setNeedsStatusRefresh(true)
      }
      if (!cancelled) setPreviewInvalidated(true)
    } catch {
      setPreviewInvalidated(true)
      setNeedsStatusRefresh(true)
      setStatus(null)
      setCallError("清除请求响应无法安全验证，结果未确认，可能已经受理。输入和预览已保留，但不能确认数据是否发生变化；请先刷新维护状态。")
    } finally {
      setBusy(null)
    }
  }

  async function refreshStatus() {
    if (!available || busy) return
    setBusy("status")
    setCallError(null)
    try {
      const result = await readDesktopDataClearStatus()
      setStatus(result)
      setNeedsStatusRefresh(dataClearStatusNeedsRefresh(result))
      setApplyResult(null)
    } catch {
      setNeedsStatusRefresh(true)
      setCallError("无法读取清除维护状态；结果仍未确认，请再次刷新。不能据此判断清除已完成或数据未发生变化。")
    }
    finally { setBusy(null) }
  }

  function cancelLocalClear() {
    if (busy || prepared || needsStatusRefresh) return
    setPreview(null)
    setConfirmationText("")
    setApplyResult(null)
    setPreviewInvalidated(false)
    setCallError(`已放弃本次本地确认输入；未发出新的删除请求。此前维护结果仍以最新状态为准。${showStoppedServiceNotice ? stoppedPreviewCancellationNotice : ""}`)
  }

  if (!available) return <div className={styles.clearUnavailable} role="status">
    <AlertTriangle size={18} />
    <div><strong>本机数据清除不可用</strong><p>当前环境缺少 <code>runtime.clearData.preview/apply/status</code> 桌面清除接口；不会通过浏览器代替本机清除，也不会模拟清除成功。</p></div>
  </div>

  return <div className={styles.clearPanel}>
    <div className={styles.clearIntro}>
      <ShieldAlert size={20} />
      <div><strong>独立危险操作：清除当前 Agora 本机资料</strong><p>此入口不属于备份、恢复、升级、卸载或普通关闭。预览会先安全停止本应用服务以冻结范围，但不会删除内容。</p></div>
    </div>

    <div className={styles.clearDisclosure}>
      <p>只有核对预览、输入精确中文短语，并通过默认取消的第二次原生确认后，主进程才可受理。最终清理由退出后的固定执行者完成。</p>
      <p>目标外只保留不含论文、笔记、账号、会话、凭据或其他业务内容的最小维护回执，用于记录操作状态和阻止未完成维护被掩盖。</p>
    </div>

    {lifecycle.blocked && <div className={styles.clearBlocked} role="alert"><AlertTriangle size={17} /><strong>{lifecycle.message}</strong></div>}

    <div className={styles.clearActions}>
      <button type="button" className={styles.clearPreviewButton} disabled={lifecycle.blocked || busy !== null || prepared || needsStatusRefresh} onClick={() => void runPreview()}>
        {busy === "preview" ? <LoaderCircle className={styles.spinner} size={16} /> : <Trash2 size={16} />}安全停止并预览清除范围
      </button>
      <button type="button" className={styles.secondaryButton} disabled={busy !== null} onClick={() => void refreshStatus()}>
        {busy === "status" ? <LoaderCircle className={styles.spinner} size={16} /> : <RefreshCw size={16} />}刷新清除状态
      </button>
    </div>

    {status && <DataClearStatus result={status} />}
    {needsStatusRefresh && <p className={styles.clearError} role="alert"><AlertTriangle size={16} />请先刷新维护状态。结果未确认前，不能重新提交、重新预览或将本地取消当作撤销已受理的清除。</p>}
    {callError && <p className={styles.clearError} role="alert"><AlertTriangle size={16} />{callError}</p>}

    {preview?.ok && preview.scope && <div className={styles.clearPreview} aria-label="本机数据清除预览">
      <div className={styles.clearPreviewHeader}><div><strong>真实清除范围</strong><span>预览到期：{formatTimestamp(preview.expiresAt)}</span></div><span className={styles.badge}>{preview.stopped ? "服务已安全停止" : "停止状态未知"}</span></div>
      <dl className={styles.clearFacts}>
        <div><dt>当前本机资料目录</dt><dd><code>{preview.scope.profilePath}</code></dd></div>
        <div><dt>范围统计</dt><dd>{preview.scope.fileCount} 个文件 · {formatBytes(preview.scope.totalBytes)}</dd></div>
        <div><dt>凭据策略</dt><dd>{preview.scope.credentialPolicy}</dd></div>
      </dl>
      <ScopeList title="将清除" items={preview.scope.includes} />
      <ScopeList title="明确保留" items={preview.scope.preserves} />
      {expired && <p className={styles.clearError} role="alert"><AlertTriangle size={16} />此预览已过期，不能提交；请重新安全停止并获取新预览。</p>}
      {previewInvalidated && !prepared && <p className={styles.clearError} role="alert"><AlertTriangle size={16} />此一次性预览不能再次提交。请先核对最新维护状态；只有宿主允许重新清除时，才能重新预览并确认。</p>}
      <label className={styles.clearConfirmationField}>
        <span>精确输入 <code>{DATA_CLEAR_CONFIRMATION_TEXT}</code></span>
        <input aria-label="精确确认文本" value={confirmationText} disabled={busy !== null || prepared} onChange={event => setConfirmationText(event.target.value)} autoComplete="off" spellCheck={false} />
      </label>
      {confirmationText && !phraseMatches && <p className={styles.clearMismatch}>输入不完全匹配；不会提交清除请求。</p>}
      <div className={styles.clearConfirmActions}>
        <button type="button" className={styles.secondaryButton} disabled={busy !== null || prepared || needsStatusRefresh} onClick={cancelLocalClear}><X size={16} />取消本次清除</button>
        <button type="button" className={styles.clearApplyButton} disabled={!canApply} onClick={() => void runApply()}>
          {busy === "apply" ? <LoaderCircle className={styles.spinner} size={16} /> : <ShieldAlert size={16} />}提交并等待第二次原生确认
        </button>
      </div>
    </div>}

    {applyResult && <DataClearApplyResult result={applyResult} showStoppedServiceNotice={showStoppedServiceNotice} />}
  </div>
}

function lifecycleBlock(hasProtectedDrafts: boolean, activeTaskCount?: number): LifecycleBlock {
  if (hasProtectedDrafts) return { blocked: true, message: "存在未应用设置或其他未保存草稿。请先保存或明确放弃；不会静默丢弃后再停止服务。" }
  if (activeTaskCount === undefined) return { blocked: true, message: "无法确认当前活动任务数量。为保护尚未核实的草稿和任务，清除预览保持禁用。" }
  if (!Number.isSafeInteger(activeTaskCount) || activeTaskCount < 0) return { blocked: true, message: "活动任务状态无效。为保护尚未核实的草稿和任务，清除预览保持禁用。" }
  if (activeTaskCount > 0) return { blocked: true, message: `当前有 ${activeTaskCount} 个活动任务。请等待任务结束或先安全取消；不会为清除强制终止。` }
  return { blocked: false, message: null }
}

function ScopeList({ title, items }: { title: string; items: string[] }) {
  return <div className={styles.clearScopeList}><strong>{title}</strong><ul>{items.map((item, index) => <li key={`${title}-${index}`}>{item}</li>)}</ul></div>
}

function dataClearStatusNeedsRefresh(result: DesktopDataClearStatusResult): boolean {
  const knownState = ["IDLE", "NONE", "PREVIEW", "PREPARED", "FINALIZING", "FAILED_PARTIAL", "CLEARED"].includes(result.state)
  return !knownState || (!result.ok && !["PREPARED", "FINALIZING", "FAILED_PARTIAL"].includes(result.state))
}

function isConfirmedDataClearCancellation(result: DesktopDataClearApplyResult): boolean {
  return !result.ok && result.state === "CANCELLED" && result.cancelled === true && result.exitRequired !== true && !result.operationId
}

function isDataClearRejectedBeforeAuthorization(result: DesktopDataClearApplyResult): boolean {
  return !result.ok && result.state === "REJECTED" && result.code === "desktop_data_clear_confirmation_mismatch" && result.exitRequired !== true && !result.operationId
}

function DataClearApplyResult({ result, showStoppedServiceNotice }: { result: DesktopDataClearApplyResult; showStoppedServiceNotice: boolean }) {
  const prepared = result.state === "PREPARED"
  const cancelled = isConfirmedDataClearCancellation(result)
  const rejectedBeforeAuthorization = isDataClearRejectedBeforeAuthorization(result)
  const text = prepared
    ? result.ok
      ? "已受理、退出后清理，尚未完成。"
      : "已准备清除，尚未完成；宿主退出或清理程序尚未就绪，请先刷新维护状态。"
    : result.state === "FAILED_PARTIAL"
      ? "清除发生部分失败，尚未完成；部分数据可能已删除，请先刷新维护状态。"
      : cancelled
        ? "原生确认已取消；本次请求未执行删除，输入和预览已保留。"
        : rejectedBeforeAuthorization
          ? "清除授权被拒绝；本次请求未执行删除，输入已保留。"
          : "清除结果未确认，可能已经受理；请先刷新维护状态。"
  return <div className={prepared ? styles.clearPrepared : styles.clearResult} role="status">
    {prepared && result.ok ? <CheckCircle2 size={18} /> : <AlertTriangle size={18} />}
    <div><strong>{text}</strong><p>结果码：<code>{result.code}</code>{prepared
      ? "。只有应用退出后的专用清理程序完成核验，并在维护状态中确认后，才能报告清除完成。"
      : cancelled || rejectedBeforeAuthorization
        ? "。如需继续，请重新核对清除范围和授权。"
        : "。不能据此判断数据未发生变化，也不能认定操作已经撤销。"}</p>
      {showStoppedServiceNotice && (cancelled || rejectedBeforeAuthorization) && <p>{stoppedPreviewCancellationNotice}</p>}
    </div>
  </div>
}

function DataClearStatus({ result }: { result: DesktopDataClearStatusResult }) {
  const final = result.ok && result.state === "CLEARED" && Boolean(result.completedAt)
  const prepared = result.state === "PREPARED"
  const label = final
    ? "退出后的固定执行者已报告清除完成。"
    : prepared
      ? result.ok ? "已受理、退出后清理，尚未完成。" : "已准备清除，尚未完成；请先刷新维护状态。"
      : result.state === "FINALIZING"
        ? "退出后清理正在进行，尚未完成。"
        : result.state === "FAILED_PARTIAL"
          ? "上次清除发生部分失败，部分数据可能已删除；不会自动继续删除。请核对维护状态后重新预览并再次明确确认。"
          : result.ok && (result.state === "IDLE" || result.state === "NONE")
            ? "当前没有待完成的清除操作。"
            : result.ok && result.state === "PREVIEW"
              ? "当前只有清除预览，尚未确认删除。"
              : "清除维护结果未确认，请先刷新维护状态。"
  return <div className={final ? styles.clearFinal : styles.clearStatus} role="status"><div><strong>{label}</strong><p>状态：<code>{result.state}</code> · 结果码：<code>{result.code}</code>{final && result.completedAt ? ` · 完成时间：${formatTimestamp(result.completedAt)}` : ""}</p></div></div>
}

function formatTimestamp(value?: string): string {
  if (!value) return "未报告"
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return "时间无效"
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "medium" }).format(parsed)
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`
  const units = ["KB", "MB", "GB", "TB"]
  let amount = value / 1024
  let unit = units[0]
  for (let index = 1; amount >= 1024 && index < units.length; index += 1) {
    amount /= 1024
    unit = units[index]
  }
  return `${amount.toFixed(amount >= 10 ? 1 : 2)} ${unit}`
}









