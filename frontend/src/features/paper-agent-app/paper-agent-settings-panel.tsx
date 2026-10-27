"use client"

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { AlertTriangle, CheckCircle2, CircleDot, Database, Gauge, HardDrive, LoaderCircle, RefreshCw, RotateCcw, ShieldCheck, Trash2, Wifi, X, type LucideIcon } from "lucide-react"
import { fetchDesktopCapabilities, fetchDesktopIdentity, type DesktopCapabilities, type DesktopIdentity } from "@/lib/research/desktop/client"
import { getPersonalMemoryInfo, getPersonalMemoryPolicy, type PersonalMemoryInfo, type PersonalMemoryPolicy } from "./personal-memory-client"
import { PaperAgentDataClearPanel } from "./paper-agent-data-clear-panel"
import { activateDesktopRestore, applyDesktopConfig, configDraftFromView, createDesktopBackup, hasDesktopConfigBridge, hasDesktopCredentialBridge, hasDesktopMaintenanceBridge, hasDesktopRuntimeBridge, inspectDesktopCredential, previewDesktopConfig, previewDesktopRestore, readDesktopHostSnapshot, removeDesktopCredential, replaceDesktopCredential, retryDesktopHostRuntime, showDesktopConfig, validateDesktopConfigDraft, type DesktopBackupResult, type DesktopConfigApplyResult, type DesktopConfigDraft, type DesktopConfigDraftErrors, type DesktopConfigPreviewResult, type DesktopConfigView, type DesktopCredentialResult, type DesktopHostSnapshot, type DesktopRestoreActivateResult, type DesktopRestorePreview, type DesktopRuntimeStatus } from "./paper-agent-settings-runtime"
import styles from "./paper-agent-settings-panel.module.css"

type LoadState<T> = { value: T | null; error: boolean }
type HostState =
  | { kind: "loading" }
  | { kind: "browser" }
  | { kind: "desktop"; snapshot: Extract<DesktopHostSnapshot, { kind: "desktop" }> }
  | { kind: "error" }

const empty = <T,>(): LoadState<T> => ({ value: null, error: false })

/** Sections reachable from the dialog navigation; `id` is the section element id, `${id}-title` its heading. */
const settingsSections: ReadonlyArray<{ id: string; label: string; icon: LucideIcon }> = [
  { id: "settings-overview", label: "运行概览", icon: Gauge },
  { id: "settings-data", label: "数据位置", icon: Database },
  { id: "settings-capabilities", label: "能力与配置", icon: CircleDot },
  { id: "settings-network", label: "联网与记忆授权", icon: Wifi },
  { id: "settings-host", label: "宿主诊断与备份", icon: HardDrive },
  { id: "settings-clear", label: "清除本机数据", icon: Trash2 },
]
const cx = (...values: Array<string | false | undefined>) => values.filter(Boolean).join(" ")

export function PaperAgentSettingsModal({
  open,
  overlayClassName,
  hasOtherDrafts = false,
  activeTaskCount,
  onRequestClose,
  onDraftChange,
}: {
  open: boolean
  overlayClassName?: string
  hasOtherDrafts?: boolean
  activeTaskCount?: number
  onRequestClose: () => void
  onDraftChange?: (dirty: boolean) => void
}) {
  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return
      event.preventDefault()
      event.stopImmediatePropagation()
      onRequestClose()
    }
    document.addEventListener("keydown", onKeyDown, true)
    return () => document.removeEventListener("keydown", onKeyDown, true)
  }, [onRequestClose, open])

  if (!open) return null
  return <div className={overlayClassName} role="presentation" onMouseDown={event => {
    if (event.target === event.currentTarget) onRequestClose()
  }}>
    <PaperAgentSettingsPanel modal hasOtherDrafts={hasOtherDrafts} activeTaskCount={activeTaskCount} onClose={onRequestClose} onDraftChange={onDraftChange} />
  </div>
}

export function PaperAgentSettingsPanel({ onClose, onDraftChange, hasOtherDrafts = false, activeTaskCount, modal = false, className }: { onClose?: () => void; onDraftChange?: (dirty: boolean) => void; hasOtherDrafts?: boolean; activeTaskCount?: number; modal?: boolean; className?: string }) {
  const panelRef = useRef<HTMLElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  useSettingsModalFocus(modal, panelRef, closeRef)
  const requestRef = useRef(0)
  const [identity, setIdentity] = useState<LoadState<DesktopIdentity>>(empty)
  const [capabilities, setCapabilities] = useState<LoadState<DesktopCapabilities>>(empty)
  const [memoryInfo, setMemoryInfo] = useState<LoadState<PersonalMemoryInfo>>(empty)
  const [memoryPolicy, setMemoryPolicy] = useState<LoadState<PersonalMemoryPolicy>>(empty)
  const [host, setHost] = useState<HostState>({ kind: "loading" })
  const [loading, setLoading] = useState(true)
  const [retrying, setRetrying] = useState(false)
  const [configDraftDirty, setConfigDraftDirty] = useState(false)
  // Forward the draft state in the same pass the form reports it, so the owner's close
  // guard never sees a stale value while a dirty draft is already on screen.
  const reportConfigDraft = useCallback((dirty: boolean) => {
    setConfigDraftDirty(dirty)
    onDraftChange?.(dirty)
  }, [onDraftChange])

  const load = useCallback(async (signal?: AbortSignal) => {
    const requestId = ++requestRef.current
    setLoading(true)
    const results = await Promise.allSettled([
      fetchDesktopIdentity(signal),
      fetchDesktopCapabilities(signal),
      getPersonalMemoryInfo(signal),
      getPersonalMemoryPolicy(signal),
      readDesktopHostSnapshot(),
    ] as const)
    if (signal?.aborted || requestId !== requestRef.current) return
    setIdentity(stateFrom(results[0]))
    setCapabilities(stateFrom(results[1]))
    setMemoryInfo(stateFrom(results[2]))
    setMemoryPolicy(stateFrom(results[3]))
    const hostResult = results[4]
    if (hostResult.status === "rejected") setHost(hasDesktopRuntimeBridge() ? { kind: "error" } : { kind: "browser" })
    else if (hostResult.value.kind === "browser") setHost({ kind: "browser" })
    else setHost({ kind: "desktop", snapshot: hostResult.value })
    setLoading(false)
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => { controller.abort(); requestRef.current += 1 }
  }, [load])

  async function retryRuntime() {
    if (retrying) return
    setRetrying(true)
    try {
      const snapshot = await retryDesktopHostRuntime()
      setHost(snapshot.kind === "desktop" ? { kind: "desktop", snapshot } : { kind: "browser" })
    } catch {
      setHost({ kind: "error" })
    } finally {
      setRetrying(false)
    }
  }

  const memoryRemoteAllowed = memoryPolicy.value?.allow_remote_processing === true
  return (
    <section ref={panelRef} className={cx(styles.panel, styles.navigable, className)} role={modal ? "dialog" : undefined} aria-modal={modal || undefined} aria-labelledby="paper-agent-settings-title" tabIndex={modal ? -1 : undefined}>
      <SettingsNavigation contentRef={contentRef} />
      <div ref={contentRef} className={styles.content}>
      <header className={styles.header} id="settings-overview">
        <div><p className={styles.eyebrow}>本机设置与可靠性</p><h2 id="paper-agent-settings-title">运行状态、数据边界与诊断</h2><p>只展示受控服务返回的真实状态；不会读取或显示 API 密钥。</p></div>
        <div className={styles.headerActions}>
          <button type="button" className={styles.secondaryButton} disabled={loading} onClick={() => void load()}>{loading ? <LoaderCircle className={styles.spinner} size={16} /> : <RefreshCw size={16} />}刷新全部状态</button>
          {onClose && <button ref={closeRef} type="button" className={styles.iconButton} aria-label="关闭设置面板" onClick={onClose}><X size={18} /></button>}
        </div>
      </header>

      <div className={styles.summaryGrid}>
        <SummaryCard icon={<HardDrive size={18} />} label="宿主运行" value={hostSummary(host)} tone={hostTone(host)} />
        <SummaryCard icon={<Database size={18} />} label="个人记忆" value={memoryInfo.error ? "状态读取失败" : memoryInfo.value?.status === "available" ? "本机存储可用" : loading ? "正在检查" : "状态未知"} tone={memoryInfo.error ? "danger" : memoryInfo.value?.status === "available" ? "success" : "neutral"} />
        <SummaryCard icon={<ShieldCheck size={18} />} label="记忆远程处理" value={memoryPolicy.error ? "策略读取失败" : memoryRemoteAllowed ? "已由用户授权" : memoryPolicy.value ? "未授权（关闭）" : "正在检查"} tone={memoryPolicy.error ? "danger" : memoryRemoteAllowed ? "warning" : memoryPolicy.value ? "success" : "neutral"} />
      </div>

      <div className={styles.sections}>
        <section className={styles.card} id="settings-data" aria-labelledby="settings-data-title">
          <SectionHeading icon={<Database size={18} />} title="真实数据位置" id="settings-data-title" />
          {memoryInfo.error && <InlineError>无法读取个人记忆数据位置；未使用猜测路径替代。</InlineError>}
          <dl className={styles.definitionList}>
            <Definition label="个人记忆数据目录" value={memoryInfo.value?.data_root} pending={loading} />
            <Definition label="个人记忆数据库" value={memoryInfo.value?.database_path} pending={loading} />
            <Definition label="存储实现" value={memoryInfo.value?.storage} pending={loading} />
            <Definition label="远程处理默认值" value={policyDefaultLabel(memoryInfo.value)} pending={loading} />
            <Definition label="自动开启远程处理" value={policyAutomaticLabel(memoryInfo.value)} pending={loading} />
            <Definition label="会话所有者范围" value={identity.value?.owner.memoryNamespace} pending={loading} error={identity.error} />
          </dl>
          <Notice icon={<ShieldCheck size={17} />} title="数据分类：本机个人研究数据">包括论文、笔记、运行记录与个人记忆。所有者来自已验证会话，不能由页面 payload 指定。</Notice>
        </section>

        <section className={styles.card} id="settings-capabilities" aria-labelledby="settings-capabilities-title">
          <SectionHeading icon={<CircleDot size={18} />} title="实际能力与待配置状态" id="settings-capabilities-title" />
          <p className={styles.sectionLead}>“已配置”只说明组件存在；只有受控业务操作留下验证记录时才标记“已验证”。</p>
          {capabilities.error && <InlineError>无法读取能力清单；不会把配置存在误报为可用。</InlineError>}
          {capabilities.value && <ul className={styles.capabilityList}>{capabilities.value.capabilities.map(capability => {
            const view = capabilityPresentation(capability)
            return <li key={capability.id}><div className={styles.capabilityMain}><span className={styles.capabilityName}>{capability.label}</span><span className={cx(styles.badge, styles[view.tone])}>{view.label}</span><span className={styles.requirement}>{capability.required ? "必要能力" : "可选能力"}</span></div><p>{capability.message || view.description}</p>{capability.verifiedAt && <small>最近业务验证：{formatTime(capability.verifiedAt)}</small>}</li>
          })}</ul>}
          {!capabilities.value && !capabilities.error && <LoadingLine />}
          <RuntimeConfigurationForm hasOtherDrafts={hasOtherDrafts} onDraftChange={reportConfigDraft} onRuntimeStatusChange={status => setHost(previous => status && previous.kind === "desktop" ? { kind: "desktop", snapshot: { ...previous.snapshot, status, diagnostics: { ...previous.snapshot.diagnostics, runtime: status } } } : { kind: "error" })} />
        </section>

        <section className={styles.card} id="settings-network" aria-labelledby="settings-network-title">
          <SectionHeading icon={<Wifi size={18} />} title="联网范围与个人记忆授权" id="settings-network-title" />
          {memoryPolicy.error ? <InlineError>无法确认个人记忆远程处理策略；按未授权处理，不能回传伪成功。</InlineError> : memoryPolicy.value ? (
            <div className={cx(styles.policyState, memoryRemoteAllowed ? styles.policyWarning : styles.policySafe)}>{memoryRemoteAllowed ? <AlertTriangle size={18} /> : <CheckCircle2 size={18} />}<div><strong>{memoryRemoteAllowed ? "个人记忆远程处理已授权" : "个人记忆远程处理未授权"}</strong><p>权威策略 revision {memoryPolicy.value.revision}。{memoryRemoteAllowed ? "只有任务明确选择的 active 记忆才具备发送资格。" : "默认关闭；配置存在、界面勾选或 capability 状态都不会自动开启。"}</p></div></div>
          ) : <LoadingLine />}
          <div className={styles.dataTable} role="table" aria-label="联网目的与数据分类">
            <div className={styles.dataTableHeader} role="row"><span role="columnheader">动作</span><span role="columnheader">必要目的</span><span role="columnheader">可能涉及的数据</span></div>
            <div role="row"><strong role="cell">本机浏览与管理</strong><span role="cell">阅读、检索本机资料</span><span role="cell">论文、笔记、个人记忆；保持本机处理</span></div>
            <div role="row"><strong role="cell">显式研究动作</strong><span role="cell">来源检索、分析或问答</span><span role="cell">完成动作所需的问题、论文片段与证据</span></div>
            <div role="row"><strong role="cell">个人记忆辅助</strong><span role="cell">按用户偏好调整任务</span><span role="cell">仅策略允许且任务明确选择的 active 记录</span></div>
          </div>
          <p className={styles.footnote}>此面板本身不会发起模型、检索或 embedding 调用，也不会因为检测到配置而开启联网权限。</p>
        </section>

        <section className={styles.card} id="settings-host" aria-labelledby="settings-host-title">
          <SectionHeading icon={<HardDrive size={18} />} title="本机宿主诊断" id="settings-host-title" />
          <HostDiagnostics host={host} retrying={retrying} onRetry={() => void retryRuntime()} />
          <MaintenancePanel host={host} />
        </section>

        <section className={cx(styles.card, styles.clearCard)} id="settings-clear" aria-labelledby="settings-clear-title">
          <div className={styles.sectionHeading}><ShieldCheck size={18} /><h3 id="settings-clear-title">清除本机 Agora 数据</h3></div>
          <PaperAgentDataClearPanel hasProtectedDrafts={hasOtherDrafts || configDraftDirty} activeTaskCount={activeTaskCount} />
        </section>
      </div>
      </div>
    </section>
  )
}

/** Left navigation of the settings dialog: jumps to a section and tracks the one being read. */
function SettingsNavigation({ contentRef }: { contentRef: { current: HTMLDivElement | null } }) {
  const [activeId, setActiveId] = useState(settingsSections[0].id)
  useEffect(() => {
    const root = contentRef.current
    if (!root || typeof IntersectionObserver === "undefined") return
    const visible = new Map<string, number>()
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (entry.isIntersecting) visible.set(entry.target.id, entry.boundingClientRect.top)
        else visible.delete(entry.target.id)
      }
      const first = settingsSections.find(section => visible.has(section.id))
      if (first) setActiveId(first.id)
    }, { root, rootMargin: "0px 0px -55% 0px" })
    for (const section of settingsSections) {
      const element = root.querySelector<HTMLElement>(`#${section.id}`)
      if (element) observer.observe(element)
    }
    return () => observer.disconnect()
  }, [contentRef])

  function jumpTo(id: string) {
    const target = contentRef.current?.querySelector<HTMLElement>(`#${id}`)
    if (!target) return
    setActiveId(id)
    const reducedMotion = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches
    target.scrollIntoView?.({ behavior: reducedMotion ? "auto" : "smooth", block: "start" })
  }

  return (
    <nav className={styles.nav} aria-label="设置分区">
      <p className={styles.navTitle}>设置</p>
      {settingsSections.map(section => (
        <button key={section.id} type="button" className={styles.navItem} aria-current={activeId === section.id ? "true" : undefined} onClick={() => jumpTo(section.id)}>
          <section.icon size={16} strokeWidth={1.8} aria-hidden="true" />
          <span>{section.label}</span>
        </button>
      ))}
    </nav>
  )
}

const emptyConfigDraft: DesktopConfigDraft = { llmEnabled: false, routeId: "writer-primary", model: "", baseUrl: "", sourceApiUrl: "https://export.arxiv.org/api/query", sourceTimeoutSeconds: "90" }
type ConfigBusy = "load" | "preview" | "apply" | "credential-replace" | "credential-remove" | null


/** Keep the modal's background unavailable until unmount, then restore the opener. */
function useSettingsModalFocus(modal: boolean, panelRef: { current: HTMLElement | null }, closeRef: { current: HTMLButtonElement | null }) {
  useEffect(() => {
    const panel = panelRef.current
    if (!modal || !panel) return
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const background: Array<{ element: HTMLElement; inert: string | null; ariaHidden: string | null }> = []
    // Include siblings along the ancestor chain so app controls and external page chrome
    // are hidden from assistive navigation without making the dialog itself inert.
    for (let branch: HTMLElement = panel; branch.parentElement; branch = branch.parentElement) {
      for (const sibling of Array.from(branch.parentElement.children)) {
        if (sibling === branch || !(sibling instanceof HTMLElement)) continue
        background.push({ element: sibling, inert: sibling.getAttribute("inert"), ariaHidden: sibling.getAttribute("aria-hidden") })
        sibling.setAttribute("inert", "")
        sibling.setAttribute("aria-hidden", "true")
      }
      if (branch.parentElement === document.body) break
    }
    const focusable = () => Array.from(panel.querySelectorAll<HTMLElement>(
      'button, input:not([type="hidden"]), select, textarea, a[href], summary, [tabindex], [contenteditable="true"]',
    )).filter(element => {
      if (element.tabIndex < 0 || element.matches(":disabled") || element.closest('[hidden], [inert], [aria-hidden="true"]')) return false
      for (let ancestor: HTMLElement | null = element; ancestor; ancestor = ancestor.parentElement) {
        const style = window.getComputedStyle(ancestor)
        if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") return false
        if (ancestor === panel) break
      }
      return true
    })
    let lastFocused: HTMLElement = closeRef.current ?? panel
    const focusInside = () => {
      const controls = focusable()
      const target = controls.includes(lastFocused) ? lastFocused : controls[0] ?? panel
      target.focus({ preventScroll: true })
    }
    const onFocusIn = (event: FocusEvent) => {
      if (event.target instanceof HTMLElement && panel.contains(event.target)) lastFocused = event.target
      else focusInside()
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Tab" || event.altKey || event.ctrlKey || event.metaKey) return
      event.preventDefault()
      const controls = focusable()
      const index = controls.indexOf(document.activeElement as HTMLElement)
      const nextIndex = index < 0 ? (event.shiftKey ? controls.length - 1 : 0) : (index + (event.shiftKey ? -1 : 1) + controls.length) % controls.length
      ;(controls[nextIndex] ?? panel).focus({ preventScroll: true })
    }
    document.addEventListener("focusin", onFocusIn, true)
    document.addEventListener("keydown", onKeyDown, true)
    focusInside()
    return () => {
      document.removeEventListener("focusin", onFocusIn, true)
      document.removeEventListener("keydown", onKeyDown, true)
      for (const { element, inert, ariaHidden } of background) {
        if (inert === null) element.removeAttribute("inert")
        else element.setAttribute("inert", inert)
        if (ariaHidden === null) element.removeAttribute("aria-hidden")
        else element.setAttribute("aria-hidden", ariaHidden)
      }
      if (opener?.isConnected && !opener.closest("[inert]")) opener.focus({ preventScroll: true })
    }
  }, [modal, panelRef, closeRef])
}

function RuntimeConfigurationForm({ onDraftChange, onRuntimeStatusChange, hasOtherDrafts }: { onDraftChange?: (dirty: boolean) => void; onRuntimeStatusChange?: (status: DesktopRuntimeStatus | null) => void; hasOtherDrafts: boolean }) {
  const available = hasDesktopConfigBridge()
  const credentialsAvailable = hasDesktopCredentialBridge()
  const [current, setCurrent] = useState<DesktopConfigView | null>(null)
  const [draft, setDraft] = useState<DesktopConfigDraft>(emptyConfigDraft)
  const [errors, setErrors] = useState<DesktopConfigDraftErrors>({})
  const [preview, setPreview] = useState<DesktopConfigPreviewResult | null>(null)
  const [applyResult, setApplyResult] = useState<DesktopConfigApplyResult | null>(null)
  const [credential, setCredential] = useState<DesktopCredentialResult | null>(null)
  const [busy, setBusy] = useState<ConfigBusy>(available ? "load" : null)
  const [loadFailed, setLoadFailed] = useState(false)
  const baseline = useMemo(() => current ? configDraftFromView(current) : null, [current])
  const dirty = baseline !== null && !sameConfigDraft(draft, baseline)

  // Layout effect: the owner learns about the draft before the dirty notice is painted.
  useLayoutEffect(() => { onDraftChange?.(dirty) }, [dirty, onDraftChange])
  useEffect(() => {
    if (!available) return
    let active = true
    Promise.allSettled([showDesktopConfig(), credentialsAvailable ? inspectDesktopCredential() : Promise.resolve(null)]).then(results => {
      if (!active) return
      const configResult = results[0]
      if (configResult.status === "fulfilled" && configResult.value.ok && configResult.value.configuration) {
        setCurrent(configResult.value.configuration)
        setDraft(configDraftFromView(configResult.value.configuration))
      } else setLoadFailed(true)
      if (results[1].status === "fulfilled" && results[1].value) setCredential(results[1].value)
      setBusy(null)
    })
    return () => { active = false }
  }, [available, credentialsAvailable])

  function update<K extends keyof DesktopConfigDraft>(field: K, value: DesktopConfigDraft[K]) {
    setDraft(previous => ({ ...previous, [field]: value }))
    setErrors(previous => ({ ...previous, [field]: undefined }))
    setPreview(null); setApplyResult(null)
  }
  async function runPreview() {
    if (busy) return
    const validated = validateDesktopConfigDraft(draft)
    setErrors(validated.errors)
    if (!validated.candidate) return
    setBusy("preview"); setApplyResult(null)
    try { setPreview(await previewDesktopConfig(validated.candidate)) }
    catch { setPreview({ ok: false, state: "ERROR", code: "desktop_config_preview_failed", message: "配置预览失败。" }) }
    finally { setBusy(null) }
  }
  async function runApply() {
    if (busy || !preview?.ok || !preview.previewId) return
    setBusy("apply")
    try {
      const received = await applyDesktopConfig(preview.previewId)
      const result = received.ok && !received.configuration
        ? { ...received, ok: false, state: "UNKNOWN", code: "desktop_config_apply_protocol_invalid", message: "宿主未返回应用后的权威配置。" }
        : received
      setApplyResult(result)
      if (result.ok && result.configuration) {
        setCurrent(result.configuration); setDraft(configDraftFromView(result.configuration)); setErrors({}); setPreview(null)
        onRuntimeStatusChange?.(result.runtime ?? null)
      } else {
        onDraftChange?.(true)
        if (!result.cancelled) setPreview(null)
      }
    } catch {
      onDraftChange?.(true)
      setApplyResult({ ok: false, state: "ERROR", code: "desktop_config_apply_failed", message: "配置应用失败。" })
      setPreview(null)
    } finally { setBusy(null) }
  }
  async function runCredential(action: "replace" | "remove") {
    if (busy || !credentialsAvailable) return
    setBusy(action === "replace" ? "credential-replace" : "credential-remove")
    try { setCredential(action === "replace" ? await replaceDesktopCredential() : await removeDesktopCredential()) }
    catch { setCredential({ ok: false, code: "desktop_credential_operation_failed", configured: credential?.configured ?? false }) }
    finally { setBusy(null) }
  }

  if (!available) return <Notice warning icon={<AlertTriangle size={17} />} title="浏览器没有运行配置能力">当前页面没有 native typed bridge，模型与论文来源配置不可编辑。不会创建浏览器 fallback 或模拟应用成功。</Notice>
  if (busy === "load") return <LoadingLine />
  if (loadFailed || !current) return <div><InlineError>无法从本机配置 owner 读取当前配置；为避免覆盖未知 revision，编辑保持禁用。</InlineError></div>

  const credentialConfigured = credential?.ok ? credential.configured : current.research.llm?.credentialConfigured ?? null
  return <div className={styles.configEditor}>
    <div className={styles.configStatus}>
      <div><strong>Research 运行配置</strong><span>当前 revision {current.revision} · 保存配置不等于 provider 已通过真实业务验证。</span></div>
      <span className={cx(styles.badge, dirty ? styles.badgeWarning : styles.badgeNeutral)}>{dirty ? "有待应用修改" : "与当前配置一致"}</span>
    </div>
    <fieldset className={styles.configFieldset} disabled={busy !== null}>
      <legend>模型路由</legend>
      <label className={styles.checkField}><input type="checkbox" checked={draft.llmEnabled} onChange={event => update("llmEnabled", event.target.checked)} />启用 Research 模型路由</label>
      <div className={styles.fixedFacts}><span>provider <code>openai-compatible</code></span><span>凭据绑定由宿主固定，renderer 不可修改</span></div>
      <div className={styles.formGrid}>
        <ConfigField label="路由 ID" error={errors.routeId}><input value={draft.routeId} disabled={!draft.llmEnabled} onChange={event => update("routeId", event.target.value)} autoComplete="off" /></ConfigField>
        <ConfigField label="模型" error={errors.model}><input value={draft.model} disabled={!draft.llmEnabled} onChange={event => update("model", event.target.value)} autoComplete="off" /></ConfigField>
        <ConfigField wide label="API Base URL" error={errors.baseUrl}><input value={draft.baseUrl} disabled={!draft.llmEnabled} onChange={event => update("baseUrl", event.target.value)} inputMode="url" autoComplete="off" /></ConfigField>
      </div>
    </fieldset>
    <fieldset className={styles.configFieldset} disabled={busy !== null}>
      <legend>论文来源</legend>
      <div className={styles.fixedFacts}><span>preset <code>arxiv-default</code></span><span>只影响 Research 的 Arxiv provider</span></div>
      <div className={styles.formGrid}>
        <ConfigField wide label="Arxiv API URL" error={errors.sourceApiUrl}><input value={draft.sourceApiUrl} onChange={event => update("sourceApiUrl", event.target.value)} inputMode="url" autoComplete="off" /></ConfigField>
        <ConfigField label="超时（秒）" error={errors.sourceTimeoutSeconds}><input type="number" min="1" max="300" step="1" value={draft.sourceTimeoutSeconds} onChange={event => update("sourceTimeoutSeconds", event.target.value)} /></ConfigField>
      </div>
    </fieldset>
    <div className={styles.credentialBox}>
      <div><strong>系统凭据槽位</strong><span>{credentialConfigured === true ? "已配置（未验证）" : credentialConfigured === false ? "未配置" : "状态未知"}；密钥值不会进入 renderer。</span>{credential && <small>结果码：<code>{credential.code}</code>{credential.cancelled ? " · 操作已取消" : ""}</small>}</div>
      <div className={styles.maintenanceActions}><button type="button" className={styles.secondaryButton} disabled={!credentialsAvailable || busy !== null} onClick={() => void runCredential("replace")}>在系统窗口中{credentialConfigured ? "替换" : "录入"}</button><button type="button" className={styles.secondaryButton} disabled={!credentialsAvailable || credentialConfigured !== true || busy !== null} onClick={() => void runCredential("remove")}>删除系统凭据</button></div>
    </div>
    {!credentialsAvailable && <InlineError>native credential bridge 不可用；不能检查、录入或删除凭据。</InlineError>}
    <div className={styles.configActions}><button type="button" className={styles.secondaryButton} disabled={!dirty || busy !== null} onClick={() => { setDraft(configDraftFromView(current)); setErrors({}); setPreview(null); setApplyResult(null) }}>放弃修改</button><button type="button" className={styles.primaryButton} disabled={!dirty || busy !== null} onClick={() => void runPreview()}>{busy === "preview" ? <LoaderCircle className={styles.spinner} size={16} /> : <ShieldCheck size={16} />}校验并预览</button></div>
    {hasOtherDrafts && <p className={styles.draftWarning}>工作区中另有未保存草稿，请先核对。应用配置仍需 native host 确认；设置应用或放弃设置修改不会清除其他草稿。</p>}
    {preview && <ConfigPreview preview={preview} applying={busy === "apply"} onApply={() => void runApply()} />}
    {applyResult && <ConfigOperationResult result={applyResult} />}
  </div>
}

function ConfigField({ label, error, wide = false, children }: { label: string; error?: string; wide?: boolean; children: ReactNode }) { return <label className={wide ? styles.wideField : undefined}><span>{label}</span>{children}{error && <small role="alert">{error}</small>}</label> }
function ConfigPreview({ preview, applying, onApply }: { preview: DesktopConfigPreviewResult; applying: boolean; onApply: () => void }) {
  if (!preview.ok || !preview.previewId || !preview.configuration) return <div className={cx(styles.configResult, styles.gapNotice)} role="status"><strong>预览未创建，输入仍保留。</strong><span>结果码：<code>{preview.code}</code></span></div>
  const target = preview.configuration
  const scope = preview.effectScope
  const scopeAccurate = scope?.sourceConsumers.includes("research-arxiv-provider") === true && scope.notChanged.includes("guided-paper-search-public-cache")
  return <div className={styles.configPreview} role="status"><strong>配置已通过 owner 校验，尚未应用</strong><span>目标 revision {target.revision}；模型 {target.research.llm ? `${target.research.llm.routeId} / ${target.research.llm.model}` : "关闭"}；来源 Arxiv 超时 {target.research.source.timeoutSeconds} 秒。</span>{scopeAccurate ? <span>真实影响范围：{scope.llmConsumers.join("、") || "无模型消费者"}；来源只影响 <code>research-arxiv-provider</code>。<code>guided-paper-search-public-cache</code> 不会随此来源设置改变。</span> : <span className={styles.draftWarning}>宿主未返回可核对的 Research Arxiv 影响范围，不能应用。</span>}<button type="button" className={styles.primaryButton} disabled={applying || !scopeAccurate} onClick={onApply}>{applying ? <LoaderCircle className={styles.spinner} size={16} /> : <ShieldCheck size={16} />}在 native 确认后应用</button></div>
}
function ConfigOperationResult({ result }: { result: DesktopConfigApplyResult }) {
  const success = result.ok
  const cancelled = result.cancelled === true
  const successLabel = result.restartRequired
    ? result.runtime?.phase === "stopped"
      ? "配置已保存并重新绑定；受控 runtime 已停止。关闭并重新打开 App，或使用宿主重试，才会以新配置进入运行态。provider 仍需真实业务验证。"
      : "配置已保存并重新绑定；请重新打开 App 或刷新宿主状态后再使用远程能力。provider 仍需真实业务验证。"
    : "宿主已确认配置应用完成。provider 仍需真实业务验证。"
  return <div className={cx(styles.configResult, success ? styles.policySafe : cancelled ? styles.notice : styles.gapNotice)} role="status"><strong>{success ? successLabel : cancelled ? "已取消应用；输入和当前配置均保留。" : "应用未完成；输入已保留，可重新预览。"}</strong><span>结果码：<code>{result.code}</code>{result.runtime ? ` · runtime：${phaseLabel(result.runtime.phase)}` : ""}</span></div>
}
function sameConfigDraft(left: DesktopConfigDraft, right: DesktopConfigDraft): boolean { return Object.keys(left).every(key => left[key as keyof DesktopConfigDraft] === right[key as keyof DesktopConfigDraft]) }

function SummaryCard({ icon, label, value, tone }: { icon: ReactNode; label: string; value: string; tone: "success" | "warning" | "danger" | "neutral" }) {
  return <div className={cx(styles.summaryCard, styles[tone])}>{icon}<div><span>{label}</span><strong>{value}</strong></div></div>
}
function SectionHeading({ icon, title, id }: { icon: ReactNode; title: string; id: string }) { return <div className={styles.sectionHeading}>{icon}<h3 id={id}>{title}</h3></div> }
function Definition({ label, value, pending, error = false }: { label: string; value?: string; pending: boolean; error?: boolean }) { return <div><dt>{label}</dt><dd><code>{error ? "读取失败" : value || (pending ? "正在读取…" : "服务未返回")}</code></dd></div> }
function InlineError({ children }: { children: ReactNode }) { return <p className={styles.inlineError} role="alert"><AlertTriangle size={16} />{children}</p> }
function LoadingLine() { return <p className={styles.loadingLine}><LoaderCircle className={styles.spinner} size={16} />正在读取受控服务状态…</p> }
function Notice({ icon, title, children, warning = false }: { icon: ReactNode; title: string; children: ReactNode; warning?: boolean }) { return <div className={warning ? styles.gapNotice : styles.notice}>{icon}<div><strong>{title}</strong><p>{children}</p></div></div> }

function HostDiagnostics({ host, retrying, onRetry }: { host: HostState; retrying: boolean; onRetry: () => void }) {
  if (host.kind === "loading") return <LoadingLine />
  if (host.kind === "browser") return <div className={styles.browserNotice}><AlertTriangle size={18} /><div><strong>当前仅在浏览器中运行</strong><p>宿主 runtime.status、retry 与 diagnostics 不可用；浏览器页面不会模拟这些本机能力。</p></div></div>
  if (host.kind === "error") return <div><InlineError>本机宿主桥接存在，但诊断读取失败。未将浏览器连通性当作宿主健康状态。</InlineError><button type="button" className={styles.secondaryButton} onClick={onRetry} disabled={retrying}>{retrying ? <LoaderCircle className={styles.spinner} size={16} /> : <RotateCcw size={16} />}重试宿主服务</button></div>
  const { status, diagnostics } = host.snapshot
  return <div className={styles.hostBody}>
    <div className={styles.hostStatus}><span className={cx(styles.phaseDot, styles[phaseTone(status)])} aria-hidden="true" /><div><strong>{phaseLabel(status.phase)}</strong><p>状态码：<code>{status.code}</code> · 检查时间：{formatTime(status.checkedAt)}</p></div></div>
    <dl className={styles.hostFacts}><div><dt>宿主版本</dt><dd>{diagnostics.hostVersion}</dd></div><div><dt>运行形态</dt><dd>{diagnostics.packaged ? "已打包桌面应用" : "本机开发宿主"}</dd></div><div><dt>本机传输</dt><dd>{status.localTransportReady ? "已验证可用" : "不可用或未验证"}</dd></div><div><dt>受信本机入口</dt><dd><code>{status.canonicalOrigin || "未提供"}</code></dd></div></dl>
    <div className={styles.hostActions}><button type="button" className={styles.secondaryButton} onClick={onRetry} disabled={retrying || !status.canRetry}>{retrying ? <LoaderCircle className={styles.spinner} size={16} /> : <RotateCcw size={16} />}{status.canRetry ? "重试宿主服务" : "当前无需重试"}</button></div>
    <div className={styles.events}><strong>最近诊断事件</strong>{diagnostics.recentEvents.length === 0 ? <p>宿主尚未记录诊断事件。</p> : <ul>{diagnostics.recentEvents.slice(-5).reverse().map((event, index) => <li key={[event.at, event.command, index].join("-")}><time>{formatTime(event.at)}</time><code>{event.command}</code><span>{event.state}</span><code>{event.code}</code></li>)}</ul>}</div>
  </div>
}


type MaintenanceBusy = "backup" | "preview" | "activate" | null

function MaintenancePanel({ host }: { host: HostState }) {
  const [busy, setBusy] = useState<MaintenanceBusy>(null)
  const [backup, setBackup] = useState<DesktopBackupResult | null>(null)
  const [preview, setPreview] = useState<DesktopRestorePreview | null>(null)
  const [activation, setActivation] = useState<DesktopRestoreActivateResult | null>(null)
  const [failed, setFailed] = useState(false)
  const available = host.kind === "desktop" && hasDesktopMaintenanceBridge()

  async function runBackup() {
    if (busy) return
    setBusy("backup"); setFailed(false); setBackup(null)
    try { setBackup(await createDesktopBackup()) }
    catch { setFailed(true) }
    finally { setBusy(null) }
  }

  async function runPreview() {
    if (busy) return
    setBusy("preview"); setFailed(false); setPreview(null); setActivation(null)
    try { setPreview(await previewDesktopRestore()) }
    catch { setFailed(true) }
    finally { setBusy(null) }
  }

  async function runActivation() {
    if (busy || !preview?.previewId || preview.isolationVerified !== true) return
    setBusy("activate"); setFailed(false); setActivation(null)
    try { setActivation(await activateDesktopRestore(preview.previewId)) }
    catch { setFailed(true) }
    finally { setBusy(null) }
  }

  if (!available) return <Notice warning icon={<AlertTriangle size={17} />} title="备份与恢复仅在桌面宿主可用">当前环境没有完整的受控 maintenance bridge；不会显示伪造的备份或恢复成功状态。用户导出不等同完整备份。</Notice>

  return <div className={styles.maintenanceBox}>
    <div className={styles.maintenanceActions}>
      <button type="button" className={styles.secondaryButton} disabled={busy !== null} onClick={() => void runBackup()}>{busy === "backup" ? <LoaderCircle className={styles.spinner} size={16} /> : <HardDrive size={16} />}创建本机备份</button>
      <button type="button" className={styles.secondaryButton} disabled={busy !== null} onClick={() => void runPreview()}>{busy === "preview" ? <LoaderCircle className={styles.spinner} size={16} /> : <ShieldCheck size={16} />}选择备份并隔离预览</button>
    </div>
    <p className={styles.footnote}>路径由主进程原生选择器控制。恢复必须先通过隔离预览；preview receipt 短期有效且只能激活一次，个人记忆恢复还需要宿主原生确认。</p>
    {failed && <InlineError>宿主维护调用失败；没有报告成功，也没有修改当前数据。</InlineError>}
    {backup && <MaintenanceResult ok={backup.ok} cancelled={backup.cancelled} code={backup.code} success="宿主已确认备份完成。">
      {backup.ok && backup.backup ? <span>文件：{backup.backup.fileName}；文件数：{backup.backup.fileCount ?? "未报告"}；字节数：{backup.backup.totalBytes ?? "未报告"}</span> : null}
    </MaintenanceResult>}
    {preview && <MaintenanceResult ok={preview.ok && preview.isolationVerified === true} cancelled={preview.cancelled} code={preview.code} success="备份已通过隔离恢复预览，尚未激活。">
      {preview.ok && preview.previewId && preview.isolationVerified === true ? <div className={styles.restorePreview}><span>备份标识：<code>{preview.backupId || "未报告"}</code></span><span>预览有效期：{preview.expiresAt ? formatTime(preview.expiresAt) : "未报告"}</span>{preview.personalMemory && <span>个人记忆记录：{Object.values(preview.personalMemory.statusCounts).reduce((total, count) => total + count, 0)}；策略：{preview.personalMemory.policyCount}。{preview.personalMemory.warning}</span>}<button type="button" className={styles.secondaryButton} disabled={busy !== null} onClick={() => void runActivation()}>{busy === "activate" ? <LoaderCircle className={styles.spinner} size={16} /> : <ShieldCheck size={16} />}激活已验证快照</button></div> : null}
    </MaintenanceResult>}
    {activation && <MaintenanceResult ok={activation.ok} cancelled={activation.cancelled} code={activation.code} success="宿主已确认恢复快照激活完成；旧会话按恢复策略失效。" />}
  </div>
}

function MaintenanceResult({ ok, cancelled, code, success, children }: { ok: boolean; cancelled?: boolean; code: string; success: string; children?: ReactNode }) {
  const label = ok ? success : cancelled ? "操作已取消；当前数据保持不变。" : "操作未完成；当前状态未被报告为成功。"
  return <div className={cx(styles.maintenanceResult, ok ? styles.policySafe : cancelled ? styles.notice : styles.gapNotice)} role="status"><div><strong>{label}</strong><p>结果码：<code>{code}</code></p>{children}</div></div>
}

function policyDefaultLabel(info: PersonalMemoryInfo | null): string | undefined { if (!info || info.remote_processing_default === undefined) return undefined; return info.remote_processing_default ? "默认开启" : "默认关闭" }
function policyAutomaticLabel(info: PersonalMemoryInfo | null): string | undefined { if (!info || info.remote_processing_auto_enabled === undefined) return undefined; return info.remote_processing_auto_enabled ? "服务报告可自动开启" : "不会自动开启" }
function stateFrom<T>(result: PromiseSettledResult<T>): LoadState<T> { return result.status === "fulfilled" ? { value: result.value, error: false } : { value: null, error: true } }
function capabilityPresentation(capability: DesktopCapabilities["capabilities"][number]) {
  if (capability.verified && capability.reachable) return { label: "已验证", tone: "badgeSuccess" as const, description: "已由受控业务操作验证。" }
  if (capability.verified) return { label: "已验证 · 当前不可达", tone: "badgeDanger" as const, description: "存在验证记录，但当前不可达。" }
  if (capability.configured) return { label: "已配置 · 待业务验证", tone: "badgeWarning" as const, description: "配置存在，但尚无当前 generation 的业务验证。" }
  return { label: "待配置", tone: "badgeNeutral" as const, description: "运行组件尚未配置。" }
}
function hostSummary(host: HostState): string { if (host.kind === "loading") return "正在检查"; if (host.kind === "browser") return "浏览器模式"; if (host.kind === "error") return "诊断读取失败"; return phaseLabel(host.snapshot.status.phase) }
function hostTone(host: HostState): "success" | "warning" | "danger" | "neutral" { if (host.kind === "browser") return "warning"; if (host.kind === "error") return "danger"; if (host.kind !== "desktop") return "neutral"; const phase = host.snapshot.status.phase; if (phase === "ready") return "success"; if (phase === "starting" || phase === "degraded") return "warning"; return "danger" }
function phaseLabel(phase: DesktopRuntimeStatus["phase"]): string { return ({ ready: "宿主已就绪", starting: "宿主启动中", stopped: "宿主已停止", degraded: "宿主降级运行", error: "宿主错误", unknown: "宿主状态未知" })[phase] }
function phaseTone(status: DesktopRuntimeStatus): "success" | "warning" | "danger" | "neutral" { if (status.phase === "ready") return "success"; if (status.phase === "starting" || status.phase === "degraded") return "warning"; if (status.phase === "unknown") return "neutral"; return "danger" }
function formatTime(value: string): string { const parsed = new Date(value); if (Number.isNaN(parsed.getTime())) return "时间未知"; return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "medium" }).format(parsed) }








