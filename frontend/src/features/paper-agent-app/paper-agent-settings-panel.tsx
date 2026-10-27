"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { AlertTriangle, CheckCircle2, CircleDot, KeyRound, LoaderCircle, RefreshCw, ShieldCheck, UserRound, Users, Wifi, X, type LucideIcon } from "lucide-react"
import { usePortalAccount } from "@/components/auth/portal-account-provider"
import type { ModelCredentialStatus } from "@/lib/account/client"
import type { SettingsSection } from "@/lib/account/settings-navigation"
import { fetchDesktopCapabilities, type DesktopCapabilities } from "@/lib/research/desktop/client"
import { getPersonalMemoryPolicy, type PersonalMemoryPolicy } from "./personal-memory-client"
import { AccountAdminSection } from "./settings/account-admin-section"
import { AccountPasswordSection } from "./settings/account-password-section"
import { credentialStatusView, ModelCredentialSection } from "./settings/model-credential-section"
import { cx, formatTime, InlineError, LoadingLine, SectionHeading, StatusBadge, SummaryCard, type Tone } from "./settings/settings-ui"
import styles from "./paper-agent-settings-panel.module.css"

type LoadState<T> = { value: T | null; error: boolean }
type NavigationEntry = { section: SettingsSection; label: string; icon: LucideIcon }

const empty = <T,>(): LoadState<T> => ({ value: null, error: false })
const sectionId = (section: SettingsSection) => `settings-${section}`

const navigation: ReadonlyArray<NavigationEntry & { adminOnly?: boolean }> = [
  { section: "account", label: "账号与密码", icon: UserRound },
  { section: "model", label: "模型 API", icon: KeyRound },
  { section: "capabilities", label: "能力状态", icon: CircleDot },
  { section: "network", label: "联网与记忆授权", icon: Wifi },
  { section: "admin", label: "账号管理", icon: Users, adminOnly: true },
]

export function PaperAgentSettingsModal({
  open,
  overlayClassName,
  initialSection,
  onRequestClose,
  onDraftChange,
}: {
  open: boolean
  overlayClassName?: string
  initialSection?: SettingsSection
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
    <PaperAgentSettingsPanel modal initialSection={initialSection} onClose={onRequestClose} onDraftChange={onDraftChange} />
  </div>
}

export function PaperAgentSettingsPanel({ onClose, onDraftChange, initialSection, modal = false, className }: { onClose?: () => void; onDraftChange?: (dirty: boolean) => void; initialSection?: SettingsSection; modal?: boolean; className?: string }) {
  const account = usePortalAccount()
  const user = account?.session?.user ?? null
  const isAdmin = user?.role === "admin"
  const sections = navigation.filter(entry => !entry.adminOnly || isAdmin)
  const panelRef = useRef<HTMLElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  useSettingsModalFocus(modal, panelRef, closeRef)
  const requestRef = useRef(0)
  const [capabilities, setCapabilities] = useState<LoadState<DesktopCapabilities>>(empty)
  const [memoryPolicy, setMemoryPolicy] = useState<LoadState<PersonalMemoryPolicy>>(empty)
  const [credentialStatus, setCredentialStatus] = useState<ModelCredentialStatus | null | undefined>(undefined)
  const [loading, setLoading] = useState(true)
  const reportDraft = useSectionDrafts(onDraftChange)
  const reportPasswordDraft = useCallback((dirty: boolean) => reportDraft("account", dirty), [reportDraft])
  const reportModelDraft = useCallback((dirty: boolean) => reportDraft("model", dirty), [reportDraft])
  const reportAdminDraft = useCallback((dirty: boolean) => reportDraft("admin", dirty), [reportDraft])

  const load = useCallback(async (signal?: AbortSignal) => {
    const requestId = ++requestRef.current
    setLoading(true)
    const results = await Promise.allSettled([fetchDesktopCapabilities(signal), getPersonalMemoryPolicy(signal)] as const)
    if (signal?.aborted || requestId !== requestRef.current) return
    setCapabilities(stateFrom(results[0]))
    setMemoryPolicy(stateFrom(results[1]))
    setLoading(false)
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => { controller.abort(); requestRef.current += 1 }
  }, [load])

  useEffect(() => {
    if (!initialSection) return
    contentRef.current?.querySelector<HTMLElement>(`#${sectionId(initialSection)}`)?.scrollIntoView?.({ block: "start" })
  }, [initialSection])

  const memoryRemoteAllowed = memoryPolicy.value?.allow_remote_processing === true
  const credentialView = credentialStatus ? credentialStatusView[credentialStatus] : null
  return (
    <section ref={panelRef} className={cx(styles.panel, styles.navigable, className)} role={modal ? "dialog" : undefined} aria-modal={modal || undefined} aria-labelledby="paper-agent-settings-title" tabIndex={modal ? -1 : undefined}>
      <SettingsNavigation contentRef={contentRef} sections={sections} initialSection={initialSection} />
      <div ref={contentRef} className={styles.content}>
      <header className={styles.header}>
        <div><p className={styles.eyebrow}>设置</p><h2 id="paper-agent-settings-title">账号、模型与数据授权</h2><p>你的论文、笔记和个人记忆保存在服务器上你自己的账号空间里，其他账号无法读取。</p></div>
        <div className={styles.headerActions}>
          <button type="button" className={styles.secondaryButton} disabled={loading} onClick={() => void load()}>{loading ? <LoaderCircle className={styles.spinner} size={16} /> : <RefreshCw size={16} />}刷新状态</button>
          {onClose && <button ref={closeRef} type="button" className={styles.iconButton} aria-label="关闭设置面板" onClick={onClose}><X size={18} /></button>}
        </div>
      </header>

      <div className={styles.summaryGrid}>
        <SummaryCard icon={<UserRound size={18} />} label="当前账号" value={user ? `${user.username} · ${isAdmin ? "管理员" : "普通用户"}` : "未登录"} tone={user ? "success" : "warning"} />
        <SummaryCard icon={<KeyRound size={18} />} label="模型 API" value={credentialStatus === undefined ? "正在检查" : credentialView?.label ?? "状态读取失败"} tone={credentialStatus === undefined ? "neutral" : credentialView?.tone ?? "danger"} />
        <SummaryCard icon={<ShieldCheck size={18} />} label="记忆远程处理" value={memoryPolicy.error ? "策略读取失败" : memoryRemoteAllowed ? "已由你授权" : memoryPolicy.value ? "未授权（关闭）" : "正在检查"} tone={memoryPolicy.error ? "danger" : memoryRemoteAllowed ? "warning" : memoryPolicy.value ? "success" : "neutral"} />
      </div>

      <div className={styles.sections}>
        <section className={styles.card} id={sectionId("account")} aria-labelledby="settings-account-title">
          <SectionHeading icon={<UserRound size={18} />} title="账号与密码" id="settings-account-title" />
          {user && <dl className={styles.definitionList}>
            <div><dt>用户名</dt><dd>{user.username}</dd></div>
            <div><dt>角色</dt><dd>{isAdmin ? "管理员" : "普通用户"}</dd></div>
            {user.createdAt && <div><dt>创建时间</dt><dd>{formatTime(user.createdAt)}</dd></div>}
          </dl>}
          <AccountPasswordSection onDraftChange={reportPasswordDraft} />
          {account && <div className={styles.configActions}><button type="button" className={styles.secondaryButton} disabled={account.pending} onClick={() => void account.signOut()}>退出登录</button></div>}
        </section>

        <section className={styles.card} id={sectionId("model")} aria-labelledby="settings-model-title">
          <SectionHeading icon={<KeyRound size={18} />} title="模型 API" id="settings-model-title" />
          <p className={styles.sectionLead}>分析、问答等需要大模型的功能使用你自己的 API Key 调用，费用计入你的模型服务账户。</p>
          <ModelCredentialSection onDraftChange={reportModelDraft} onStatusChange={setCredentialStatus} />
        </section>

        <section className={styles.card} id={sectionId("capabilities")} aria-labelledby="settings-capabilities-title">
          <SectionHeading icon={<CircleDot size={18} />} title="能力状态" id="settings-capabilities-title" />
          <p className={styles.sectionLead}>“已配置”只说明服务端组件存在；只有真实业务操作成功后才标记“已验证”。</p>
          {capabilities.error && <InlineError>无法读取能力清单；不会把配置存在误报为可用。</InlineError>}
          {capabilities.value && <ul className={styles.capabilityList}>{capabilities.value.capabilities.map(capability => {
            const view = capabilityPresentation(capability)
            return <li key={capability.id}><div className={styles.capabilityMain}><span className={styles.capabilityName}>{capability.label}</span><StatusBadge tone={view.tone}>{view.label}</StatusBadge><span className={styles.requirement}>{capability.required ? "必要能力" : "可选能力"}</span></div><p>{capability.message || view.description}</p>{capability.verifiedAt && <small>最近业务验证：{formatTime(capability.verifiedAt)}</small>}</li>
          })}</ul>}
          {!capabilities.value && !capabilities.error && <LoadingLine />}
        </section>

        <section className={styles.card} id={sectionId("network")} aria-labelledby="settings-network-title">
          <SectionHeading icon={<Wifi size={18} />} title="联网范围与个人记忆授权" id="settings-network-title" />
          {memoryPolicy.error ? <InlineError>无法确认个人记忆远程处理策略；按未授权处理。</InlineError> : memoryPolicy.value ? (
            <div className={cx(styles.policyState, memoryRemoteAllowed ? styles.policyWarning : styles.policySafe)}>{memoryRemoteAllowed ? <AlertTriangle size={18} /> : <CheckCircle2 size={18} />}<div><strong>{memoryRemoteAllowed ? "个人记忆远程处理已授权" : "个人记忆远程处理未授权"}</strong><p>{memoryRemoteAllowed ? "只有任务明确选择的已启用记忆才会随请求发送给模型服务。" : "默认关闭；可以在“个人记忆”里修改这项授权。"}</p></div></div>
          ) : <LoadingLine />}
          <div className={styles.dataTable} role="table" aria-label="联网目的与数据分类">
            <div className={styles.dataTableHeader} role="row"><span role="columnheader">动作</span><span role="columnheader">目的</span><span role="columnheader">可能涉及的数据</span></div>
            <div role="row"><strong role="cell">浏览与管理</strong><span role="cell">阅读、筛选你的论文库</span><span role="cell">论文、笔记、个人记忆；只在服务器上处理，不发给模型服务</span></div>
            <div role="row"><strong role="cell">显式研究动作</strong><span role="cell">来源检索、分析或问答</span><span role="cell">完成动作所需的问题、论文片段与证据，经你的 API Key 发给模型服务</span></div>
            <div role="row"><strong role="cell">个人记忆辅助</strong><span role="cell">按你的偏好调整任务</span><span role="cell">仅在授权开启且任务明确选择时，附带已启用的记忆</span></div>
          </div>
        </section>

        {isAdmin && user && <section className={styles.card} id={sectionId("admin")} aria-labelledby="settings-admin-title">
          <SectionHeading icon={<Users size={18} />} title="账号管理" id="settings-admin-title" />
          <p className={styles.sectionLead}>这里只有管理员可见。新账号由管理员创建，初始密码只显示一次。</p>
          <AccountAdminSection currentUserId={user.userId} onDraftChange={reportAdminDraft} />
        </section>}
      </div>
      </div>
    </section>
  )
}

/** Combines each section's unsaved-input state into the dialog's single draft flag. */
function useSectionDrafts(onDraftChange?: (dirty: boolean) => void) {
  const drafts = useRef(new Set<SettingsSection>())
  return useCallback((section: SettingsSection, dirty: boolean) => {
    const before = drafts.current.size > 0
    if (dirty) drafts.current.add(section)
    else drafts.current.delete(section)
    const after = drafts.current.size > 0
    if (before !== after) onDraftChange?.(after)
  }, [onDraftChange])
}

/** Left navigation of the settings dialog: jumps to a section and tracks the one being read. */
function SettingsNavigation({ contentRef, sections, initialSection }: { contentRef: { current: HTMLDivElement | null }; sections: ReadonlyArray<NavigationEntry>; initialSection?: SettingsSection }) {
  const [activeId, setActiveId] = useState(sectionId(initialSection ?? sections[0].section))
  useEffect(() => {
    const root = contentRef.current
    if (!root || typeof IntersectionObserver === "undefined") return
    const visible = new Map<string, number>()
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (entry.isIntersecting) visible.set(entry.target.id, entry.boundingClientRect.top)
        else visible.delete(entry.target.id)
      }
      const first = sections.find(entry => visible.has(sectionId(entry.section)))
      if (first) setActiveId(sectionId(first.section))
    }, { root, rootMargin: "0px 0px -55% 0px" })
    for (const entry of sections) {
      const element = root.querySelector<HTMLElement>(`#${sectionId(entry.section)}`)
      if (element) observer.observe(element)
    }
    return () => observer.disconnect()
  }, [contentRef, sections])

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
      {sections.map(entry => {
        const id = sectionId(entry.section)
        return <button key={id} type="button" className={styles.navItem} aria-current={activeId === id ? "true" : undefined} onClick={() => jumpTo(id)}>
          <entry.icon size={16} strokeWidth={1.8} aria-hidden="true" />
          <span>{entry.label}</span>
        </button>
      })}
    </nav>
  )
}

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

function stateFrom<T>(result: PromiseSettledResult<T>): LoadState<T> { return result.status === "fulfilled" ? { value: result.value, error: false } : { value: null, error: true } }
function capabilityPresentation(capability: DesktopCapabilities["capabilities"][number]): { label: string; tone: Tone; description: string } {
  if (capability.verified && capability.reachable) return { label: "已验证", tone: "success", description: "已由真实业务操作验证。" }
  if (capability.verified) return { label: "已验证 · 当前不可达", tone: "danger", description: "存在验证记录，但当前不可达。" }
  if (capability.configured) return { label: "已配置 · 待业务验证", tone: "warning", description: "配置存在，但还没有成功的业务操作。" }
  return { label: "待配置", tone: "neutral", description: "该能力尚未配置。" }
}
