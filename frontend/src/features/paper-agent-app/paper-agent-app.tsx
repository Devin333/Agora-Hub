"use client"

import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from "react"
import { Bookmark, BookOpen, Brain, ChevronLeft, FilePlus2, FolderPen, Library, LoaderCircle, Menu, PanelLeftClose, Plus, RefreshCw, Search, Settings, Sparkles, Tag, Trash2, Wifi, WifiOff, X } from "lucide-react"
import { usePortalAccount } from "@/components/auth/portal-account-provider"
import { BrandMark } from "@/components/brand/brand-mark"
import { useAppearanceOptions } from "@/components/theme/appearance-menu"
import { ResearchSourceEntry } from "@/features/portal/components/research-source-entry"
import { modelCredentialChangedEvent } from "@/lib/account/model-credential-events"
import { openSettingsEvent, parseSettingsSection, settingsQueryKey, settingsSectionFrom, type SettingsSection } from "@/lib/account/settings-navigation"
import { fetchDesktopCapabilities, fetchDesktopIdentity, fetchResearchOperation } from "@/lib/research/desktop/client"
import type { DesktopCapabilities, DesktopIdentity } from "@/lib/research/desktop/contracts"
import { historyState, removeResearchGroup, saveResearchGroup, saveResearchMaterial } from "@/lib/research/history"
import { retryHistoryEvent } from "@/lib/research/history-sync"
import { useGuidedResearch } from "@/lib/research/use-guided-research"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import type { AppearancePreference } from "@/lib/theme/appearance"
import { useUiStore } from "@/stores/ui-store"
import type { ResearchResult } from "@/lib/research/conversation"
import { operationReceiptMatches, operationReceiptUpdate, updateResearchOperation } from "@/lib/research/pending-operations"
import type { ResearchPendingOperation } from "@/lib/research/workspace-items"
import {
  materialAccessState,
  materialFavorite,
  materialPaperId,
  materialSourceUrl,
  materialReadingStatus,
  materialSaveState,
  materialTags,
  parseTags,
  patchMaterial,
  sameResearchMaterial,
  sortMaterials,
  stableResultMaterial,
  type LibraryFilter,
  type LibrarySort,
  type MaterialSaveReceipt,
  type PaperAgentMaterial,
  type ReaderTab,
} from "./model"
import type { PersonalMemoryPolicy, PersonalMemoryRecord } from "./personal-memory-client"
import { PersonalMemoryPanel } from "./personal-memory-panel"
import { PaperAgentSettingsModal } from "./paper-agent-settings-panel"
import { PaperReaderPane } from "./paper-reader-pane"
import { useDesktopLifecycle } from "./desktop-lifecycle"
import styles from "./paper-agent-app.module.css"

const filterLabels: Record<LibraryFilter, string> = { all: "全部论文", unread: "未读", saved: "我的收藏" }
const sortLabels: Record<LibrarySort, string> = { "updated-desc": "最近更新", "created-desc": "最近入库", "title-asc": "标题 A–Z" }
const settingsDiscardMessage = "设置中仍有尚未保存的输入。关闭后将放弃这些输入，是否继续？"

export function requestSettingsModalClose(
  dirty: boolean,
  close: () => void,
  confirmDiscard: () => boolean = () => window.confirm(settingsDiscardMessage),
): boolean {
  if (dirty && !confirmDiscard()) return false
  close()
  return true
}

export function PaperAgentApp() {
  const account = usePortalAccount()
  const workspace = useOwnedResearchWorkspace()
  const guided = useGuidedResearch()
  const localSearchRef = useRef<HTMLInputElement>(null)
  const navDrawerRef = useRef<HTMLElement>(null)
  const listDrawerRef = useRef<HTMLElement>(null)
  const composing = useRef(false)
  // The resolved app-wide appearance; ThemeBridge owns `html[data-theme]`.
  const theme = useUiStore(state => state.theme)
  const [filter, setFilter] = useState<LibraryFilter>("all")
  const [sort, setSort] = useState<LibrarySort>("updated-desc")
  const [groupId, setGroupId] = useState<string | null>(null)
  const [tagFilter, setTagFilter] = useState("")
  const [query, setQuery] = useState("")
  const [onlineQuery, setOnlineQuery] = useState("")
  const [submittedOnlineQuery, setSubmittedOnlineQuery] = useState("")
  const [shortcutLabel, setShortcutLabel] = useState("Ctrl K")
  const [selectedId, setSelectedId] = useState("")
  const [assistantOpen, setAssistantOpen] = useState(false)
  const [focusMode, setFocusMode] = useState(false)
  const [readerTab, setReaderTab] = useState<ReaderTab>("original")
  const [navOpen, setNavOpen] = useState(false)
  const [listOpen, setListOpen] = useState(false)
  const [memoryOpen, setMemoryOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsSection, setSettingsSection] = useState<SettingsSection | undefined>(undefined)
  const [settingsDraftDirty, setSettingsDraftDirty] = useState(false)
  const [identity, setIdentity] = useState<DesktopIdentity | null>(null)
  const [capabilities, setCapabilities] = useState<DesktopCapabilities | null>(null)
  const [runtimeError, setRuntimeError] = useState("")
  const [onlineNotice, setOnlineNotice] = useState("")
  const [, setAuthorizedMemory] = useState<PersonalMemoryRecord[]>([])
  const [useMemory, setUseMemory] = useState(false)
  const [memoryPolicy, setMemoryPolicy] = useState<PersonalMemoryPolicy | null>(null)
  const [memoryPolicyScope, setMemoryPolicyScope] = useState("")
  const [online, setOnline] = useState(true)
  const [saveReceipts, setSaveReceipts] = useState<Record<string, MaterialSaveReceipt>>({})
  const [newGroupName, setNewGroupName] = useState("")
  const [memoryDraftDirty, setMemoryDraftDirty] = useState(false)
  const [memoryBusy, setMemoryBusy] = useState(false)
  const [readerBusy, setReaderBusy] = useState(false)
  const [readerTaskIds, setReaderTaskIds] = useState<string[]>([])
  const [readerUnsaved, setReaderUnsaved] = useState(false)
  const [checkingImportId, setCheckingImportId] = useState("")

  useDrawerFocus(navOpen, navDrawerRef)
  useDrawerFocus(listOpen, listDrawerRef)

  const owner = workspace.owner
  const memoryNamespace = identity !== null && identity.owner.userId === owner ? identity.owner.memoryNamespace : undefined
  const memoryScope = `${owner ?? "signed-out"}:${memoryNamespace ?? "unresolved"}`
  const memoryScopeRef = useRef(memoryScope)
  memoryScopeRef.current = memoryScope
  const memoryAllowed = memoryPolicyScope === memoryScope && memoryPolicy?.allow_remote_processing === true
  const taskUsesMemory = useMemory && memoryAllowed

  useEffect(() => {
    setMemoryOpen(false)
    setMemoryDraftDirty(false)
    setMemoryBusy(false)
    setAuthorizedMemory([])
    setMemoryPolicy(null)
    setMemoryPolicyScope("")
    setUseMemory(false)
  }, [memoryScope])

  const materials = useMemo(
    () => (workspace.workspace.materials ?? []).filter(item => item.kind === "paper" || item.kind === "pdf") as PaperAgentMaterial[],
    [workspace.workspace.materials],
  )
  const notebooks = useMemo(() => workspace.workspace.readerNotebooks ?? [], [workspace.workspace.readerNotebooks])
  const groups = workspace.workspace.groups
  const allTags = useMemo(() => [...new Set(materials.flatMap(materialTags))].sort((a, b) => a.localeCompare(b, "zh-CN")), [materials])

  useEffect(() => {
    setShortcutLabel(/Mac|iPhone|iPad/.test(navigator.platform) ? "⌘K" : "Ctrl K")
  }, [])

  useEffect(() => {
    const update = () => setOnline(navigator.onLine)
    update(); window.addEventListener("online", update); window.addEventListener("offline", update)
    return () => { window.removeEventListener("online", update); window.removeEventListener("offline", update) }
  }, [])

  // Bumped when the account's model key changes, so the capability summary re-reads.
  const [capabilityRevision, setCapabilityRevision] = useState(0)
  useEffect(() => {
    const onChanged = () => setCapabilityRevision(value => value + 1)
    window.addEventListener(modelCredentialChangedEvent, onChanged)
    return () => window.removeEventListener(modelCredentialChangedEvent, onChanged)
  }, [])

  useEffect(() => {
    if (!owner) { setIdentity(null); setCapabilities(null); return }
    const controller = new AbortController()
    setRuntimeError("")
    Promise.all([fetchDesktopIdentity(controller.signal), fetchDesktopCapabilities(controller.signal)])
      .then(([nextIdentity, nextCapabilities]) => {
        if (nextIdentity.owner.userId !== owner) throw new Error("研究服务返回的账号与当前登录账号不一致，已停止读取。请刷新页面或重新登录。")
        setIdentity(nextIdentity); setCapabilities(nextCapabilities)
      })
      .catch(cause => { if (!controller.signal.aborted) setRuntimeError(cause instanceof Error ? cause.message : "研究运行能力读取失败。") })
    return () => controller.abort()
  }, [capabilityRevision, owner])

  useEffect(() => {
    if (selectedId && materials.some(item => item.id === selectedId)) return
    setSelectedId(materials[0]?.id ?? "")
  }, [materials, selectedId])

  // Every close path (Escape, backdrop, close button) reads the latest reported draft state
  // from the ref, so a close request racing the dirty re-render still reaches the guard.
  const settingsDraftDirtyRef = useRef(false)
  const reportSettingsDraft = useCallback((dirty: boolean) => {
    settingsDraftDirtyRef.current = dirty
    setSettingsDraftDirty(dirty)
  }, [])
  const requestSettingsClose = useCallback(() => {
    requestSettingsModalClose(settingsDraftDirtyRef.current, () => {
      setSettingsOpen(false)
      reportSettingsDraft(false)
    })
  }, [reportSettingsDraft])
  const openSettingsAt = useCallback((section?: SettingsSection) => {
    setSettingsSection(section)
    setSettingsOpen(true)
  }, [])

  // Research surfaces ask for the section that fixes their failure (for example the model key);
  // claiming the event tells them the dialog is here, so they do not navigate.
  useEffect(() => {
    const onOpenSettings = (event: Event) => { event.preventDefault(); openSettingsAt(settingsSectionFrom(event)) }
    window.addEventListener(openSettingsEvent, onOpenSettings)
    return () => window.removeEventListener(openSettingsEvent, onOpenSettings)
  }, [openSettingsAt])

  // Pages without the dialog send the user here with ?settings=<section>; open it once signed in.
  const signedIn = Boolean(account?.session)
  useEffect(() => {
    if (!signedIn) return
    const url = new URL(window.location.href)
    const section = parseSettingsSection(url.searchParams.get(settingsQueryKey))
    if (!section) return
    url.searchParams.delete(settingsQueryKey)
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`)
    openSettingsAt(section)
  }, [openSettingsAt, signedIn])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault(); event.stopPropagation()
        if (window.matchMedia("(max-width: 1199px)").matches) { setNavOpen(false); setListOpen(true) }
        window.requestAnimationFrame(() => localSearchRef.current?.focus())
      } else if (event.key === "Escape") {
        // The settings modal owns Escape so every close path reaches the same dirty-draft
        // guard exactly once. Its document listener also contains the event before other
        // overlays can react to the same key press.
        if (settingsOpen) return
        if (memoryOpen) setMemoryOpen(false)
        else if (navOpen) setNavOpen(false)
        else if (listOpen) setListOpen(false)
        else if (assistantOpen) setAssistantOpen(false)
        else if (focusMode) setFocusMode(false)
      }
    }
    document.addEventListener("keydown", onKeyDown, true)
    return () => document.removeEventListener("keydown", onKeyDown, true)
  }, [assistantOpen, focusMode, listOpen, memoryOpen, navOpen, requestSettingsClose, settingsOpen])

  const visibleMaterials = useMemo(() => sortMaterials(materials.filter(material => {
    if (filter === "unread" && materialReadingStatus(material) !== "unread") return false
    if (filter === "saved" && !materialFavorite(material)) return false
    if (groupId && material.groupId !== groupId) return false
    if (tagFilter && !materialTags(material).some(tag => tag.toLocaleLowerCase() === tagFilter.toLocaleLowerCase())) return false
    const haystack = `${material.title} ${material.notes} ${material.url} ${material.referenceId ?? ""} ${materialTags(material).join(" ")}`.toLocaleLowerCase()
    return haystack.includes(query.trim().toLocaleLowerCase())
  }), sort), [filter, groupId, materials, query, sort, tagFilter])

  const selected = materials.find(item => item.id === selectedId) ?? null
  const selectedPaperId = selected ? materialPaperId(selected) : null
  const selectedNotebook = selectedPaperId ? notebooks.find(item => item.paperId === selectedPaperId) : undefined
  const latestTurn = guided.active?.conversation.turns.at(-1)
  const onlineResults = latestTurn?.searches.flatMap(search => search.source === "papers" ? search.results : []) ?? []
  const onlineFailures = latestTurn?.failures.filter(item => item.source === "papers") ?? []
  const unreadCount = materials.filter(material => materialReadingStatus(material) === "unread").length
  const favoriteCount = materials.filter(materialFavorite).length
  const pendingOperations = workspace.workspace.pendingOperations ?? []
  const importOperations = useMemo(() => (workspace.workspace.pendingOperations ?? []).filter(operation => operation.kind === "import" || operation.kind === "prepare").sort((left, right) => right.updatedAt - left.updatedAt).slice(0, 3), [workspace.workspace.pendingOperations])
  const pendingDesktopTaskIds = pendingOperations.filter(operation => operation.status === "waiting" || operation.status === "running" || operation.status === "unknown").map(operation => operation.requestId)
  const activeTaskCount = new Set([...pendingDesktopTaskIds, ...readerTaskIds]).size + (guided.busy ? 1 : 0) + (readerBusy ? 1 : 0) + (memoryBusy ? 1 : 0) + (checkingImportId ? 1 : 0)
  const otherDraftDirty = workspace.dirty || Boolean(onlineQuery.trim() && onlineQuery.trim() !== submittedOnlineQuery) || memoryDraftDirty || readerUnsaved
  const lifecycleDirty = otherDraftDirty || settingsDraftDirty
  const lifecycleSummary = lifecycleDirty || activeTaskCount
    ? `${settingsDraftDirty ? "设置有未保存输入" : "设置无未保存输入"}；${otherDraftDirty ? "另有未保存草稿" : "无其他未保存草稿"}；${activeTaskCount} 个活动请求`
    : undefined
  useDesktopLifecycle({ dirty: lifecycleDirty, activeTaskCount, summary: lifecycleSummary })

  const handleReaderTaskState = useCallback((state: { activeRequestIds: string[]; unsaved: boolean }) => {
    setReaderTaskIds(state.activeRequestIds)
    setReaderUnsaved(state.unsaved)
  }, [])

  const handleMemoryPolicyChange = useCallback((policy: PersonalMemoryPolicy | null) => {
    if (memoryScopeRef.current !== memoryScope) return
    setMemoryPolicy(policy)
    setMemoryPolicyScope(memoryScope)
    if (!policy?.allow_remote_processing) setUseMemory(false)
  }, [memoryScope])

  const handleMemoryDraftChange = useCallback((dirty: boolean) => {
    if (memoryScopeRef.current === memoryScope) setMemoryDraftDirty(dirty)
  }, [memoryScope])

  const handleMemoryActivityChange = useCallback((active: boolean) => {
    if (memoryScopeRef.current === memoryScope) setMemoryBusy(active)
  }, [memoryScope])

  const handleAuthorizedMemoryChange = useCallback((records: PersonalMemoryRecord[]) => {
    if (memoryScopeRef.current === memoryScope) setAuthorizedMemory(records)
  }, [memoryScope])

  const submitOnlineSearch = useCallback(() => {
    const value = onlineQuery.trim()
    if (!value || guided.busy) return
    if (!online) { setOnlineNotice("当前离线：本地筛选仍可使用，在线检索不会排队或自动补发。"); return }
    if (!owner) { setOnlineNotice("请先登录，再发起在线检索。"); return }
    setOnlineNotice("")
    setSubmittedOnlineQuery(value)
    guided.start(value, "auto", null, [], {}, ["papers"])
    queueMicrotask(() => window.history.replaceState(null, "", "/paper-agent"))
  }, [guided, online, onlineQuery, owner])

  async function checkImportOperation(operation: ResearchPendingOperation) {
    if (!online) {
      setOnlineNotice("当前离线：已保存的导入请求身份仍可查看，但不会排队或自动补发状态查询。")
      return
    }
    if (!owner || historyState().owner !== owner || checkingImportId) return
    setCheckingImportId(operation.requestId)
    setOnlineNotice("")
    try {
      const next = await fetchResearchOperation(operation.requestId, operation.generation)
      if (historyState().owner !== owner) return
      if (!operationReceiptMatches(next, operation)) {
        updateResearchOperation(operation.requestId, { status: "unknown" })
        setOnlineNotice("导入回执与已保存的 request/generation/fingerprint 不一致，未采用该结果。")
        return
      }
      const update = operationReceiptUpdate(next, operation)
      if (!update || !updateResearchOperation(operation.requestId, update, operation, owner)) {
        setOnlineNotice("导入完成回执缺少或改变了已保存的 paper/source identity，未采用该结果；请检查原请求。")
        return
      }
      setOnlineNotice(importOperationNotice(next.status, next.requestId))
    } catch (cause) {
      if (historyState().owner === owner) setOnlineNotice(cause instanceof Error ? cause.message : "导入状态暂时无法核对。")
    } finally {
      setCheckingImportId(current => current === operation.requestId ? "" : current)
    }
  }

  function selectMaterial(id: string): boolean {
    if (id !== selectedId && readerUnsaved) {
      setOnlineNotice("当前论文存在保存失败、revision 冲突或超限草稿；为避免丢失，已保留当前阅读页，请先处理该草稿。")
      return false
    }
    setSelectedId(id)
    return true
  }

  function enqueueMaterial(material: PaperAgentMaterial, successMessage?: string) {
    if (!owner) return false
    const before = historyState()
    if (!saveResearchMaterial(material, owner)) {
      setOnlineNotice(historyState().message || "论文改动未能进入研究工作区。")
      return false
    }
    const after = historyState()
    setSaveReceipts(current => ({ ...current, [material.id]: { owner, material, baseRevision: before.revision, generation: after.generation } }))
    selectMaterial(material.id)
    if (successMessage) setOnlineNotice(successMessage)
    return true
  }

  function retryMaterial(material: PaperAgentMaterial, receipt: MaterialSaveReceipt | undefined) {
    const current = historyState()
    if (!owner || current.owner !== owner || !current.ready) return
    if (receipt) {
      if (receipt.owner === owner && enqueueMaterial(receipt.material, "正在重试保存；草稿仍保留。")) window.dispatchEvent(new Event(retryHistoryEvent))
    } else if (current.workspace.materials?.some(item => item.id === material.id)) {
      // Imports bypass the local receipt map; retry the retained workspace draft, not the PDF import.
      setOnlineNotice("正在重试工作区同步；导入论文草稿仍保留。")
      window.dispatchEvent(new Event(retryHistoryEvent))
    }
  }

  async function saveResult(result: ResearchResult) {
    const existing = materials.find(material => sameResearchMaterial(material, result))
    try {
      const material = await stableResultMaterial(result, existing)
      enqueueMaterial(material, existing ? "同一规范来源已更新；等待服务端 revision 确认。" : "已加入论文库草稿；正在等待服务端 revision 确认。")
    } catch (cause) {
      setOnlineNotice(cause instanceof Error ? cause.message : "无法生成安全的论文身份。")
    }
  }

  function updateMaterial(material: PaperAgentMaterial, patch: Parameters<typeof patchMaterial>[1], message: string) {
    enqueueMaterial(patchMaterial(material, patch), message)
  }

  function editTags(material: PaperAgentMaterial) {
    const next = window.prompt("用逗号分隔标签（最多 32 个，每个 64 字符）", materialTags(material).join(", "))
    if (next === null) return
    updateMaterial(material, { tags: parseTags(next) }, "标签已进入待保存状态。")
  }

  function createGroup() {
    const value = newGroupName.trim()
    if (!value) return
    const id = saveResearchGroup(value)
    if (!id) { setOnlineNotice(historyState().message || "分组创建失败。"); return }
    setNewGroupName(""); setOnlineNotice("分组已创建，等待工作区同步确认。")
  }

  function renameGroup(id: string, name: string) {
    const value = window.prompt("修改分组名称", name)
    if (value === null) return
    if (!saveResearchGroup(value, id)) setOnlineNotice(historyState().message || "分组改名失败。")
    else setOnlineNotice("分组改名已进入待保存状态。")
  }

  function deleteGroup(id: string, name: string) {
    if (!window.confirm(`删除分组「${name}」？材料只会变为未分组，不会被删除。`)) return
    if (!removeResearchGroup(id)) setOnlineNotice(historyState().message || "分组删除失败。")
    else { if (groupId === id) setGroupId(null); setOnlineNotice("分组已删除；其中材料保留并改为未分组，等待同步确认。") }
  }

  if (!account) return <AppState title="无法初始化论文工作台" detail="请刷新页面；若仍无法打开，请重新登录或联系管理员检查服务状态。" />
  if (!account.resolved) return <AppState title="正在核对账号与论文库" detail="正在读取登录状态、owner 和工作区 revision…" busy />
  if (!account.session) return <AuthGate onLogin={element => account.openLogin(element)} error={account.error ?? workspace.message} />
  const sessionOwner = account.session.user.userId
  if (!workspace.ready || owner !== sessionOwner) {
    if (owner === sessionOwner && (workspace.status === "error" || workspace.status === "conflict")) {
      return <AppState
        title="论文库暂时无法读取"
        detail={workspace.message || "账号论文库读取失败；没有使用访客或空工作区代替真实数据。"}
        onRetry={() => window.dispatchEvent(new Event(retryHistoryEvent))}
      />
    }
    return <AppState title="正在核对账号与论文库" detail="正在读取登录状态、owner 和工作区 revision…" busy />
  }

  return <main className={styles.app} data-testid="paper-agent-app" data-paper-agent-shell data-focus={focusMode || undefined}>
    <a className={styles.skipLink} href="#paper-reader-main">跳到论文正文</a>
    <header className={styles.mobileBar}><button type="button" aria-label="打开导航" onClick={() => setNavOpen(true)}><Menu size={18} strokeWidth={1.8} /></button><strong>Agora</strong><button type="button" aria-label="打开论文列表" onClick={() => setListOpen(true)}><Library size={18} /></button></header>

    {!focusMode && <aside ref={navDrawerRef} tabIndex={-1} className={`${styles.sidebar} ${navOpen ? styles.drawerOpen : ""}`} aria-label="论文工作台导航">
      <div className={styles.brandRow}><span><BrandMark size={28} /><strong>Agora</strong></span><button type="button" className={styles.drawerClose} aria-label="关闭导航" onClick={() => setNavOpen(false)}><X size={17} /></button></div>
      <nav aria-label="论文库视图" className={styles.primaryNav}>
        <span className={styles.navLabel}>资料库</span>
        {(["all", "unread", "saved"] as const).map(item => <button type="button" className={styles.navItem} key={item} aria-current={filter === item && !groupId && !tagFilter ? "page" : undefined} data-active={filter === item && !groupId && !tagFilter || undefined} onClick={() => { setFilter(item); setGroupId(null); setTagFilter(""); setNavOpen(false) }}><span className={styles.navItemContent}>{item === "all" ? <BookOpen size={16} /> : item === "unread" ? <Menu size={16} /> : <Bookmark size={16} />}<span className={styles.navItemLabel} data-paper-agent-nav-label>{filterLabels[item]}</span></span><small>{item === "unread" ? unreadCount : item === "saved" ? favoriteCount : materials.length}</small></button>)}
        <span className={styles.navLabel}>分组</span>
        {groups.map(group => <div className={styles.navManagementRow} key={group.id}><button type="button" className={styles.navItem} data-active={groupId === group.id || undefined} onClick={() => { setGroupId(group.id); setFilter("all"); setTagFilter(""); setNavOpen(false) }}><span className={styles.navItemContent}><Library size={16} /><span className={styles.navItemLabel}>{group.name}</span></span><small>{materials.filter(item => item.groupId === group.id).length}</small></button><button type="button" aria-label={`改名分组：${group.name}`} onClick={() => renameGroup(group.id, group.name)}><FolderPen size={13} /></button><button type="button" aria-label={`删除分组：${group.name}`} onClick={() => deleteGroup(group.id, group.name)}><Trash2 size={13} /></button></div>)}
        <div className={styles.groupCreate}><input maxLength={60} value={newGroupName} onChange={event => setNewGroupName(event.target.value)} onKeyDown={event => { if (event.key === "Enter") { event.preventDefault(); createGroup() } }} placeholder="新建分组" aria-label="新分组名称" /><button type="button" aria-label="创建分组" disabled={!newGroupName.trim()} onClick={createGroup}><Plus size={14} /></button></div>
        {allTags.length > 0 && <><span className={styles.navLabel}>标签</span><div className={styles.tagNavigation}>{allTags.map(tag => <button type="button" key={tag} data-active={tagFilter === tag || undefined} onClick={() => { setTagFilter(tagFilter === tag ? "" : tag); setGroupId(null); setFilter("all"); setNavOpen(false) }}><Tag size={13} />{tag}<small>{materials.filter(item => materialTags(item).includes(tag)).length}</small></button>)}</div></>}
      </nav>
      <div className={styles.sidebarBottom}><button type="button" onClick={() => setMemoryOpen(true)}><Brain size={16} /><span>个人记忆</span><small>管理</small></button><button type="button" onClick={() => openSettingsAt()}><Settings size={16} /><span>设置</span></button><AppearanceSwitch /><div className={styles.connectionState} data-online={online || undefined}>{online ? <Wifi size={14} /> : <WifiOff size={14} />}<span>{online ? "网络已连接" : "离线模式"}</span></div></div>
    </aside>}

    {!focusMode && <section ref={listDrawerRef} tabIndex={-1} className={`${styles.libraryPane} ${listOpen ? styles.drawerOpen : ""}`} aria-label="论文列表">
      <header className={styles.libraryHeader}><h2>{tagFilter ? `# ${tagFilter}` : groupId ? groups.find(item => item.id === groupId)?.name : filterLabels[filter]}</h2><button type="button" className={styles.drawerClose} aria-label="关闭论文列表" onClick={() => setListOpen(false)}><X size={17} /></button></header>
      <label className={styles.searchBox}><Search size={15} aria-hidden="true" /><input ref={localSearchRef} type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="筛选本地论文库" aria-label="筛选本地论文库" /><kbd>{shortcutLabel}</kbd></label><div className={styles.libraryControls}><p className={styles.localHint}>本地筛选和排序不会联网。</p><label>排序<select aria-label="论文排序" value={sort} onChange={event => setSort(event.target.value as LibrarySort)}>{(Object.keys(sortLabels) as LibrarySort[]).map(value => <option key={value} value={value}>{sortLabels[value]}</option>)}</select></label></div>
      <div className={styles.materialList}>{visibleMaterials.length === 0 && <div className={styles.emptyCompact}><strong>{materials.length ? "没有匹配的论文" : "你的论文库还是空的"}</strong><p>{materials.length ? "清除筛选、标签或切换分组。" : "显式在线搜索并入库，或从本机选择 PDF。"}</p></div>}{visibleMaterials.map(material => {
        const receipt = saveReceipts[material.id]?.owner === sessionOwner ? saveReceipts[material.id] : undefined
        const saveState = materialSaveState(receipt, workspace, material, sessionOwner)
        const reading = materialReadingStatus(material)
        const access = materialAccessState(material)
        return <article className={styles.materialCard} data-active={material.id === selectedId || undefined} key={material.id}>
          <button type="button" className={styles.materialItem} aria-label={`选择论文：${material.title}`} onClick={() => { if (selectMaterial(material.id)) setListOpen(false) }}><div><strong>{material.title}</strong><p>{material.notes || material.url || "私有导入论文"}</p></div><div className={styles.materialState}><span data-save-state={saveState}>{saveState === "confirmed" ? "元数据已保存" : saveState === "pending" ? "元数据待保存" : "元数据保存失败"}</span><span data-access-state={access}>{access === "local-pdf" ? "PDF 阅读入口" : access === "reader-projection" ? "阅读入口已绑定" : "仅元数据"}</span></div></button>
          <div className={styles.materialMeta}><button type="button" aria-pressed={materialFavorite(material)} onClick={() => updateMaterial(material, { favorite: !materialFavorite(material) }, materialFavorite(material) ? "已取消收藏；论文和元数据仍保留，等待同步。" : "已加入收藏，等待同步确认。")}>{materialFavorite(material) ? <Bookmark fill="currentColor" size={13} /> : <Bookmark size={13} />}{materialFavorite(material) ? "已收藏" : "收藏"}</button><button type="button" onClick={() => updateMaterial(material, { readingStatus: reading === "read" ? "unread" : "read" }, reading === "read" ? "已标记未读，等待同步。" : "已标记已读，等待同步。")}>{reading === "read" ? "标记未读" : "标记已读"}</button><button type="button" onClick={() => editTags(material)}><Tag size={13} />标签</button>{saveState === "failed" && <button type="button" onClick={() => retryMaterial(material, receipt)}>重试保存</button>}</div>
          <div className={styles.materialTags}>{materialTags(material).map(tag => <button type="button" key={tag} onClick={() => setTagFilter(tag)}>#{tag}</button>)}</div>
          <label className={styles.materialGroup}>分组<select aria-label={`设置分组：${material.title}`} value={material.groupId ?? ""} onChange={event => updateMaterial(material, { groupId: event.target.value || null }, "分组分配已进入待保存状态。") }><option value="">未分组</option>{groups.map(group => <option key={group.id} value={group.id}>{group.name}</option>)}</select></label>
        </article>
      })}</div>
      <section className={styles.onlineSearch} aria-label="在线论文搜索"><div className={styles.onlineHeading}><Sparkles size={15} /><strong>在线搜索</strong><span>显式联网</span></div><input value={onlineQuery} onCompositionStart={() => { composing.current = true }} onCompositionEnd={() => { composing.current = false }} onChange={event => setOnlineQuery(event.target.value)} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !composing.current && !event.nativeEvent.isComposing) { event.preventDefault(); submitOnlineSearch() } }} placeholder="关键词、研究问题、arXiv / DOI / 受支持论文来源" aria-label="在线论文搜索" /><div className={styles.onlineActions}><button type="button" className={styles.primaryButton} disabled={!onlineQuery.trim() || guided.busy || !online} onClick={submitOnlineSearch}>{guided.busy ? <LoaderCircle className={styles.spinner} size={15} /> : <Search size={15} />}搜索真实来源</button><button type="button" className={styles.secondaryButton} onClick={() => window.dispatchEvent(new Event("agora-open-pdf-import"))}><FilePlus2 size={15} />导入 PDF</button></div><ResearchSourceEntry appearance="paper-agent" onAttach={id => { selectMaterial(id) }} />{importOperations.length > 0 && <section className={styles.importOperations} aria-label="已保存的导入请求"><strong>重开后可核对的导入请求</strong>{importOperations.map(operation => <div key={operation.requestId}><span>{operation.semantic.kind === "import" ? operation.semantic.filename : operation.semantic.kind === "prepare" ? operation.semantic.sourceUrl : "来源"}</span><code>{operation.requestId.slice(0, 8)}…</code><small>{importOperationStatusLabel(operation.status)}</small><button type="button" disabled={!online || Boolean(checkingImportId)} onClick={() => void checkImportOperation(operation)}>{checkingImportId === operation.requestId ? <LoaderCircle className={styles.spinner} size={13} /> : <RefreshCw size={13} />}检查已有请求</button></div>)}</section>}{(onlineNotice || guided.saveError || latestTurn?.error) && <p role="status" className={styles.onlineNotice}>{onlineNotice || guided.saveError || latestTurn?.error}</p>}{onlineFailures.map(failure => <p role="alert" className={styles.errorText} key={failure.message}>{failure.message}</p>)}{latestTurn?.phase === "results" && onlineResults.length === 0 && onlineFailures.length === 0 && <p className={styles.onlineNotice}>真实来源返回零结果；没有补造示例论文。</p>}{onlineResults.length > 0 && <div className={styles.resultList}>{onlineResults.map(result => {
        const existing = materials.find(material => sameResearchMaterial(material, result)); const receipt = existing && saveReceipts[existing.id]?.owner === sessionOwner ? saveReceipts[existing.id] : undefined; const state = existing ? materialSaveState(receipt, workspace, existing, sessionOwner) : null
        return <article key={`${result.source}:${result.id}`}><div><strong>{result.title}</strong><p>{result.authors || result.description}</p><a href={result.url} target="_blank" rel="noreferrer">{result.source}</a></div><button type="button" disabled={state === "pending" || state === "confirmed"} onClick={() => state === "failed" && existing ? retryMaterial(existing, receipt) : void saveResult(result)}>{state === "confirmed" ? "已入库" : state === "pending" ? "入库待保存" : state === "failed" ? "保存失败·重试" : "入库"}</button></article>
      })}</div>}</section>
    </section>}

    <section id="paper-reader-main" className={styles.mainPane}><div className={styles.workspaceTopbar}>{!focusMode && <><button type="button" className={styles.compactControl} aria-label="打开导航" onClick={() => setNavOpen(true)}><Menu size={16} /></button><button type="button" className={styles.compactControl} aria-label="收起或打开论文列表" onClick={() => setListOpen(value => !value)}><PanelLeftClose size={16} /></button></>}<div className={styles.capabilitySummary}><span data-ready={Boolean(identity) || undefined}>{identity ? account.session.user.username : "正在连接账号空间"}</span><span>{capabilities ? `${capabilities.capabilities.filter(item => item.verified).length}/${capabilities.capabilities.length} 能力已验证` : "能力待核对"}</span></div><label className={styles.memoryToggle} title={memoryAllowed ? "本任务是否选择已授权记忆；默认关闭。" : "请先在个人记忆中保存允许远程处理的 policy。"}><input type="checkbox" checked={taskUsesMemory} disabled={!memoryAllowed} onChange={event => setUseMemory(event.target.checked)} />使用已授权记忆</label><button type="button" className={styles.assistantButton} disabled={!selectedPaperId} aria-expanded={assistantOpen} onClick={() => setAssistantOpen(value => !value)}><Sparkles size={15} />{assistantOpen ? "关闭助手" : "打开助手"}</button></div>{runtimeError && <p role="alert" className={styles.runtimeError}>{runtimeError}</p>}{workspace.status === "conflict" || workspace.status === "error" ? <p role="alert" className={styles.runtimeError}>工作区{workspace.status === "conflict" ? "存在 revision 冲突" : "保存失败"}：{workspace.message}。待保存草稿仍保留，可在条目上重试。</p> : null}{selectedPaperId ? <PaperReaderPane key={`${memoryScope}:${selectedPaperId}`} owner={owner} paperId={selectedPaperId} online={online} onActivityChange={setReaderBusy} onTaskStateChange={handleReaderTaskState} appearance={theme} assistantOpen={assistantOpen} focusMode={focusMode} initialTab={readerTab} onAssistantClose={() => setAssistantOpen(false)} onFocusChange={setFocusMode} onTabChange={setReaderTab} useMemory={taskUsesMemory} memoryAllowed={memoryAllowed} onUseMemoryChange={setUseMemory} /> : <EmptyReader material={selected} hasMaterials={materials.length > 0} onImport={() => window.dispatchEvent(new Event("agora-open-pdf-import"))} />}</section>
    {(navOpen || listOpen) && <button type="button" className={styles.mobileScrim} aria-label="关闭抽屉" onClick={() => { setNavOpen(false); setListOpen(false) }} />}
    <PaperAgentSettingsModal open={settingsOpen} overlayClassName={styles.settingsOverlay} initialSection={settingsSection} onRequestClose={requestSettingsClose} onDraftChange={reportSettingsDraft} />
    <PersonalMemoryPanel key={memoryScope} open={memoryOpen} namespace={memoryNamespace} sourceContext={selected ? { sourceRef: selected.referenceId ?? selected.id, label: selected.title, ...(selectedNotebook?.sourceRevision ? { sourceRevision: selectedNotebook.sourceRevision } : {}) } : null} onClose={() => setMemoryOpen(false)} onAuthorizedChange={handleAuthorizedMemoryChange} onPolicyChange={handleMemoryPolicyChange} onDraftChange={handleMemoryDraftChange} onActivityChange={handleMemoryActivityChange} />
  </main>
}


function useDrawerFocus(open: boolean, drawerRef: RefObject<HTMLElement | null>) {
  const returnFocusRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (!open || !window.matchMedia("(max-width: 1199px)").matches) return
    returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const drawer = drawerRef.current
    if (!drawer) return
    const focusableSelector = 'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    const focusables = () => [...drawer.querySelectorAll<HTMLElement>(focusableSelector)].filter(element => !element.hidden && element.getAttribute("aria-hidden") !== "true")
    const frame = window.requestAnimationFrame(() => {
      if (!drawer.contains(document.activeElement)) (focusables()[0] ?? drawer).focus()
    })
    const trap = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return
      const items = focusables()
      if (!items.length) { event.preventDefault(); drawer.focus(); return }
      const first = items[0]
      const last = items.at(-1)!
      if (event.shiftKey && (document.activeElement === first || document.activeElement === drawer)) { event.preventDefault(); last.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
    }
    drawer.addEventListener("keydown", trap)
    return () => {
      window.cancelAnimationFrame(frame)
      drawer.removeEventListener("keydown", trap)
      const target = returnFocusRef.current
      if (target?.isConnected) window.requestAnimationFrame(() => target.focus())
    }
  }, [drawerRef, open])
}

const appearanceActionLabels: Record<AppearancePreference, string> = { system: "跟随系统主题", light: "使用浅色主题", dark: "使用深色主题" }

/** System / light / dark segmented control bound to the app-wide appearance preference. */
function AppearanceSwitch() {
  const { appearance, setAppearance, options, title } = useAppearanceOptions()
  return <div className={styles.themeSwitch} role="group" aria-label={title}>{options.map(option => <button type="button" key={option.value} aria-label={appearanceActionLabels[option.value]} aria-pressed={appearance === option.value} onClick={() => setAppearance(option.value)}><option.icon size={14} strokeWidth={1.8} aria-hidden="true" />{option.label}</button>)}</div>
}

// Pre-workspace screens render outside the workspace shell; they share its markers so shell-scoped styles apply.
function AppState({ title, detail, busy, onRetry }: { title: string; detail: string; busy?: boolean; onRetry?: () => void }) { return <main className={styles.statePage} data-paper-agent-shell role={busy ? "status" : "alert"}>{busy ? <LoaderCircle className={styles.spinner} size={24} /> : <BookOpen size={24} />}<h1>{title}</h1><p>{detail}</p>{onRetry && <button type="button" className={styles.primaryButton} onClick={onRetry}>重试读取论文库</button>}</main> }
function AuthGate({ onLogin, error }: { onLogin: (element: HTMLElement) => void; error?: string }) {
  return <main className={styles.authGate} data-paper-agent-shell><section><BrandMark size={74} /><h1>你的论文、原文与研究记录</h1><p>登录后读取你账号下的私有论文库、来源修订、笔记、请求恢复与报告。账号由管理员创建；没有账号请联系管理员。</p>{error && <p role="alert" className={styles.errorText}>{error}</p>}<button type="button" className={styles.primaryButton} onClick={event => onLogin(event.currentTarget)}>登录并打开论文库</button></section></main>
}
function EmptyReader({ material, hasMaterials, onImport }: { material: PaperAgentMaterial | null; hasMaterials: boolean; onImport: () => void }) {
  const source = material ? materialSourceUrl(material) : null
  if (material) return <section className={styles.emptyReader}><BookOpen size={24} /><h1>仅元数据，全文尚未入库</h1><p>“元数据已保存”不等于全文可离线阅读。可以打开受校验的论文来源，或主动选择本机 PDF；系统不会伪造正文、页码或已读状态。</p><div>{source && <a className={styles.secondaryButton} href={source} target="_blank" rel="noreferrer">打开论文来源</a>}<button type="button" className={styles.primaryButton} onClick={onImport}>导入 PDF</button></div></section>
  return <section className={styles.emptyReader}><ChevronLeft size={24} /><h1>{hasMaterials ? "选择一篇论文开始阅读" : "真实论文库为空"}</h1><p>{hasMaterials ? "列表选择会读取该 owner 下的真实 reader projection。" : "使用在线搜索入库或从本机选择 PDF；页面不会展示六篇样本或公共 catalog 充数。"}</p></section>
}

function importOperationStatusLabel(status: ResearchPendingOperation["status"]): string {
  if (status === "passed") return "处理完成"
  if (status === "failed") return "处理失败"
  if (status === "rejected") return "未通过处理门"
  if (status === "needs_action") return "需要核对"
  if (status === "unknown" || status === "interrupted") return "结果待核对"
  if (status === "running") return "处理中"
  return "等待处理"
}

function importOperationNotice(status: ResearchPendingOperation["status"], requestId: string): string {
  const identity = requestId.slice(0, 8)
  if (status === "passed") return `导入请求 ${identity}… 已由服务端确认完成；已入库论文与本地原件状态以资料库和阅读区为准。`
  if (status === "failed" || status === "rejected" || status === "needs_action") return `导入请求 ${identity}… 返回“${importOperationStatusLabel(status)}”，没有显示为可离线阅读。`
  return `导入请求 ${identity}… 当前为“${importOperationStatusLabel(status)}”；系统不会自动重新上传或补发。`
}



