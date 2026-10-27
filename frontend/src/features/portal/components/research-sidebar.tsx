"use client"

import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react"
import { Archive, BookOpen, Check, ChevronDown, ChevronRight, ClipboardCheck, Cloud, Download, Folder, FolderPlus, Github, History, Laptop, LoaderCircle, MoreHorizontal, PanelLeftClose, PanelLeftOpen, Pencil, Plus, Quote, RotateCcw, Search, Star, Trash2, UserRound, X, type LucideIcon } from "lucide-react"
import { BrandMark } from "@/components/brand/brand-mark"
import { Button } from "@/components/ui/button"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/dialog"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Input, inputClassName } from "@/components/ui/input"
import { usePortalAccount } from "@/components/auth/portal-account-provider"
import { PortalAccountControl } from "@/components/auth/portal-account-control"
import { historyState, importGuestHistory, readGuestHistory, removeResearchGroup, removeResearchVisit, restoreResearchVisit, saveResearchGroup, updateResearchVisit, type ResearchGroup, type ResearchVisit } from "@/lib/research/history"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import { reloadHistoryEvent, retryHistoryEvent } from "@/lib/research/history-sync"
import { moduleInfo, type ResearchModule } from "@/lib/research/entry"
import { researchGroupHref, researchHomeHref } from "@/lib/research/routes"
import { cn } from "@/lib/utils"

const moduleIcons: Record<ResearchModule, LucideIcon> = { papers: BookOpen, projects: Github, community: Quote, reports: ClipboardCheck }
const dateSections = ["今天", "昨天", "过去 7 天", "更早"] as const
const iconProps = { size: 15, strokeWidth: 1.8, "aria-hidden": true } as const

/** Round ghost icon button used in the sidebar header and section headers (rakazo bot-list chrome). */
const headerIconClass = "size-7 rounded-full text-muted-foreground/70 hover:text-foreground/75"
const rowClass = "group/row relative flex rounded-xl hover:bg-sidebar-accent/60"
const sectionHeaderClass = "flex items-center justify-between gap-2 px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider text-faint-foreground"
const rowMenuClass = "absolute end-1.5 top-1.5 grid size-7 place-items-center rounded-full bg-sidebar text-muted-foreground opacity-0 outline-none ring-1 ring-sidebar-border transition-opacity hover:text-foreground focus-visible:opacity-100 focus-visible:ring-3 focus-visible:ring-ring/50 group-hover/row:opacity-100 group-focus-within/row:opacity-100 data-[state=open]:opacity-100"
const focusRing = "outline-none focus-visible:ring-3 focus-visible:ring-ring/50"

type Editor = { kind: "visit"; visit: ResearchVisit } | { kind: "group"; group?: ResearchGroup } | { kind: "move"; visit: ResearchVisit } | { kind: "delete-group"; group: ResearchGroup } | { kind: "reload" }
type SaveStatus = ReturnType<typeof useOwnedResearchWorkspace>["status"]

export function researchDateSection(timestamp: number, now = new Date()) {
  const date = new Date(timestamp)
  const day = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()) - Date.UTC(date.getFullYear(), date.getMonth(), date.getDate())
  return day <= 0 ? "今天" : day === 86400000 ? "昨天" : day < 7 * 86400000 ? "过去 7 天" : "更早"
}

function visitTime(visit: ResearchVisit) {
  const date = new Date(visit.updatedAt)
  return researchDateSection(visit.updatedAt) === "今天"
    ? date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" })
}

function visitPreview(visit: ResearchVisit) {
  if (visit.activity) return visit.activity.kind === "reader" ? `阅读：${visit.activity.sectionTitle || visit.activity.title}` : `草稿：${visit.activity.title}`
  return visit.question.trim() && visit.question.trim() !== visit.title ? visit.question : ""
}

function saveStatusLabel(status: SaveStatus) {
  return status === "saving" ? "正在同步…" : status === "synced" ? "已同步到账号" : status === "loading" ? "正在加载…" : status === "error" || status === "conflict" ? "尚未保存" : "历史保存在本机"
}

export function ResearchSidebar({ collapsed, onToggle, onNew, onResume, onSelectGroup, activeVisitId, busy = false }: {
  busy?: boolean
  collapsed: boolean
  onToggle: () => void
  onNew: () => void
  onResume: (visit: ResearchVisit) => void
  onSelectGroup?: (id: string | null) => void
  /** The research currently open in the workspace; its row is shown as selected. */
  activeVisitId?: string | null
}) {
  const state = useOwnedResearchWorkspace(), account = usePortalAccount()
  const [search, setSearch] = useState("")
  const [filter, setFilter] = useState("recent")
  const [allGroups, setAllGroups] = useState(false)
  const [scope, setScope] = useState<"all" | "group">("all")
  const [showFavorites, setShowFavorites] = useState(false)
  const [editor, setEditor] = useState<Editor | null>(null)
  const [value, setValue] = useState("")
  const [editorError, setEditorError] = useState("")
  const [undo, setUndo] = useState<{ id: string; title: string } | null>(null)
  const [notice, setNotice] = useState("")
  const [importHidden, setImportHidden] = useState(false)
  const searchRef = useRef<HTMLInputElement>(null)
  const visits = state.workspace.visits.filter(v => !v.deletedAt).sort((a, b) => b.updatedAt - a.updatedAt)
  const groups = state.workspace.groups.slice().sort((a, b) => a.createdAt - b.createdAt)
  const activeGroup = groups.find(g => g.id === filter)
  const favorites = visits.filter(v => v.isFavorite && !v.archivedAt)
  const filtered = visits.filter(v => {
    if (search.trim()) return (scope === "all" || v.groupId === activeGroup?.id) && `${v.title} ${v.question} ${v.activity?.title ?? ""} ${moduleInfo[v.module].name}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase())
    if (filter === "archived") return Boolean(v.archivedAt)
    return !v.archivedAt && (filter === "recent" || (filter === "favorites" ? v.isFavorite : filter === "ungrouped" ? !v.groupId : v.groupId === filter))
  })
  const guest = state.owner && !importHidden ? readGuestHistory() : undefined
  const guestCount = guest ? guest.visits.filter(v => !v.deletedAt && !state.workspace.visits.some(item => item.id === v.id)).length + (guest.materials ?? []).filter(m => !state.workspace.materials?.some(item => item.id === m.id)).length + (guest.reportDrafts ?? []).filter(d => !state.workspace.reportDrafts?.some(item => item.id === d.id)).length + (guest.prompts ?? []).filter(p => !state.workspace.prompts?.some(item => item.id === p.id)).length : 0
  const label = search.trim() ? scope === "all" ? "全部研究 · 含归档" : `分组：${activeGroup?.name ?? ""}` : filter === "archived" ? "已归档" : filter === "favorites" ? "已收藏" : filter === "ungrouped" ? "未分组" : activeGroup?.name ?? "最近研究"

  useEffect(() => { setFilter("recent"); setScope("all"); setEditor(null); setUndo(null); setImportHidden(false); setSearch("") }, [state.owner])
  useEffect(() => { if (!activeGroup) setScope("all") }, [activeGroup])
  useEffect(() => { if (!undo) return; const timer = setTimeout(() => setUndo(null), 10000); return () => clearTimeout(timer) }, [undo])
  function openEditor(next: Editor) {
    setEditorError(""); setEditor(next)
    setValue(next.kind === "visit" ? next.visit.title : next.kind === "group" ? next.group?.name ?? "" : next.kind === "move" ? next.visit.groupId ?? "" : "")
  }
  function submitEditor(event: FormEvent) {
    event.preventDefault()
    if (!editor) return
    let success = false
    if (editor.kind === "visit") success = Boolean(value.trim()) && updateResearchVisit(editor.visit.id, { title: value.trim() })
    if (editor.kind === "group") success = Boolean(saveResearchGroup(value, editor.group?.id))
    if (editor.kind === "move") success = updateResearchVisit(editor.visit.id, { groupId: value || null })
    if (editor.kind === "delete-group") { success = removeResearchGroup(editor.group.id); if (success && filter === editor.group.id) { setFilter("recent"); onSelectGroup?.(null) } }
    if (editor.kind === "reload") { window.dispatchEvent(new Event(reloadHistoryEvent)); success = true }
    if (success) setEditor(null)
    else setEditorError(historyState().message || "名称不能为空，请检查后重试。")
  }
  function exportDraft() {
    const url = URL.createObjectURL(new Blob([JSON.stringify(state.workspace, null, 2)], { type: "application/json" }))
    const a = document.createElement("a"); a.href = url; a.download = "agora-research-history.json"; a.click(); URL.revokeObjectURL(url)
  }
  const selectView = (next: string) => { setFilter(next); if (collapsed) onToggle() }
  const openSearch = () => { if (collapsed) onToggle(); setTimeout(() => searchRef.current?.focus(), 0) }
  const newResearch = () => { setFilter("recent"); setSearch(""); onNew() }
  const views = [
    { id: "recent", label: "最近研究", icon: History, count: visits.filter(v => !v.archivedAt).length },
    { id: "favorites", label: "已收藏", icon: Star, count: visits.filter(v => v.isFavorite).length },
    { id: "archived", label: "已归档", icon: Archive, count: visits.filter(v => v.archivedAt).length },
  ]

  return <>
    <aside
      data-collapsed={collapsed || undefined}
      className={cn(
        "sticky top-0 flex h-svh min-h-[480px] shrink-0 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground",
        collapsed ? "w-[72px]" : "w-[316px]"
      )}
      aria-label="研究历史侧栏"
    >
      <div className={cn("flex items-center", collapsed ? "flex-col gap-2 px-0 pb-2 pt-4" : "justify-between gap-2 px-[18px] pb-3 pt-4")}>
        <a href={researchHomeHref()} aria-label="Agora AI 首页" title="Agora AI" className={cn("flex min-w-0 items-center gap-2.5 rounded-full", focusRing)}>
          <BrandMark size={collapsed ? 32 : 28} />
          {!collapsed && <span className="truncate text-heading3 font-semibold tracking-tight">Agora AI</span>}
        </a>
        <div className={cn("flex items-center", collapsed ? "flex-col gap-0.5" : "gap-1.5")}>
          {collapsed && <Button variant="ghost" size="icon-sm" className={headerIconClass} aria-label="搜索历史" title="搜索历史" onClick={openSearch}><Search {...iconProps} /></Button>}
          <Button variant="ghost" size="icon-sm" className={headerIconClass} disabled={busy} onClick={newResearch} aria-label="新研究" title="新研究"><Plus {...iconProps} /></Button>
          <Button variant="ghost" size="icon-sm" className={headerIconClass} onClick={onToggle} aria-label={collapsed ? "展开研究侧栏" : "收起研究侧栏"} title={collapsed ? "展开研究侧栏" : "收起研究侧栏"}>{collapsed ? <PanelLeftOpen {...iconProps} /> : <PanelLeftClose {...iconProps} />}</Button>
        </div>
      </div>

      {!collapsed && <div className="mx-2.5 mb-3 space-y-1.5">
        <label className="flex h-9 items-center gap-2 rounded-xl border border-border bg-card px-3 text-muted-foreground transition-colors focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50 dark:bg-input">
          <Search {...iconProps} />
          <input ref={searchRef} type="search" aria-label="搜索研究历史" placeholder="搜索历史" value={search} onChange={event => setSearch(event.target.value)} className="min-w-0 flex-1 bg-transparent text-body2 text-foreground outline-none placeholder:text-muted-foreground [&::-webkit-search-cancel-button]:hidden" />
          {search && <button type="button" aria-label="清空历史搜索" onClick={() => setSearch("")} className={cn("grid size-5 place-items-center rounded-full hover:text-foreground", focusRing)}><X size={13} strokeWidth={1.8} /></button>}
        </label>
        {search.trim() && activeGroup && <label className="flex items-center justify-between gap-2 px-1 text-label text-muted-foreground">搜索范围<select aria-label="历史搜索范围" value={scope} onChange={event => setScope(event.target.value as "all" | "group")} className={cn("max-w-[150px] rounded-md bg-transparent px-1 py-0.5 text-foreground", focusRing)}><option value="all">全部研究</option><option value="group">当前分组</option></select></label>}
      </div>}

      <nav aria-label="研究视图" className={cn("flex flex-col gap-0.5", collapsed ? "items-center px-0" : "px-2.5")}>
        {views.map(view => {
          const active = filter === view.id
          return <button key={view.id} type="button" aria-pressed={active} title={view.label} onClick={() => selectView(view.id)} className={cn(
            "flex items-center text-body2 transition-colors", focusRing,
            collapsed ? "size-11 justify-center rounded-xl" : "h-9 w-full gap-3 rounded-xl px-2.5",
            active ? "bg-sidebar-accent text-sidebar-accent-foreground" : "text-foreground/75 hover:bg-sidebar-accent/60 hover:text-foreground"
          )}>
            <view.icon size={16} strokeWidth={1.8} aria-hidden="true" className="shrink-0" />
            {!collapsed && <><span className="flex-1 truncate text-start">{view.label}</span><span className="text-label text-faint-foreground tabular-nums">{view.count}</span></>}
          </button>
        })}
      </nav>

      {collapsed ? <div className="flex-1" /> : <div className="rk-scroll mt-3 min-h-0 flex-1 overflow-y-auto px-2.5 pb-4">
        {favorites.length > 0 && <div className="mb-3">
          <button type="button" className={cn(sectionHeaderClass, "w-full rounded-md hover:text-muted-foreground", focusRing)} onClick={() => setShowFavorites(!showFavorites)} aria-expanded={showFavorites}>
            <span>{showFavorites ? "收起收藏快捷入口" : "展开收藏快捷入口"}</span><ChevronDown size={13} strokeWidth={1.8} aria-hidden="true" className={cn("transition-transform", showFavorites && "rotate-180")} />
          </button>
          {showFavorites && <div className="mt-0.5 flex flex-col gap-0.5">
            {favorites.slice(0, 5).map(v => <button key={v.id} type="button" className={cn("flex h-8 items-center gap-2.5 rounded-lg px-2.5 text-start text-body2 text-foreground/75 hover:bg-sidebar-accent/60 hover:text-foreground", focusRing)} onClick={() => onResume(v)} title={v.title}><Star size={13} strokeWidth={1.8} fill="currentColor" aria-hidden="true" className="shrink-0 text-faint-foreground" /><span className="truncate">{v.title}</span></button>)}
            {favorites.length > 5 && <button type="button" className={cn("px-2.5 py-1.5 text-start text-label text-muted-foreground hover:text-foreground", focusRing)} onClick={() => selectView("favorites")}>查看全部 {favorites.length} 项收藏</button>}
          </div>}
        </div>}

        <div className={sectionHeaderClass}>
          <span>我的分组</span>
          <Button variant="ghost" size="icon-xs" className="rounded-full text-muted-foreground/70 hover:text-foreground/75" title="新建分组" aria-label="新建分组" disabled={!state.ready} onClick={() => openEditor({ kind: "group" })}><FolderPlus size={14} strokeWidth={1.8} aria-hidden="true" /></Button>
        </div>
        <nav aria-label="研究分组" className="mb-4 mt-0.5 flex flex-col gap-0.5">
          {(allGroups ? groups : groups.slice(0, 5)).map(group => {
            const active = filter === group.id
            return <div className={cn(rowClass, active && "bg-sidebar-accent hover:bg-sidebar-accent")} key={group.id}>
              <button type="button" className={cn("flex h-9 min-w-0 flex-1 items-center gap-3 rounded-xl px-2.5 text-start text-body2", focusRing, active ? "text-sidebar-accent-foreground" : "text-foreground/75 hover:text-foreground")} onClick={() => selectView(group.id)} aria-current={active ? "page" : undefined} title={group.name}>
                <Folder size={16} strokeWidth={1.8} aria-hidden="true" className="shrink-0" /><span className="flex-1 truncate">{group.name}</span>
                <span className="text-label text-faint-foreground tabular-nums transition-opacity group-hover/row:opacity-0 group-focus-within/row:opacity-0">{visits.filter(v => v.groupId === group.id && !v.archivedAt).length}</span>
              </button>
              <DropdownMenu modal={false}>
                <DropdownMenuTrigger className={cn(rowMenuClass, "top-1")} aria-label={`分组操作：${group.name}`}><MoreHorizontal {...iconProps} /></DropdownMenuTrigger>
                <DropdownMenuContent className="min-w-44" side="right" align="start">
                  <DropdownMenuItem asChild><a href={researchGroupHref(group.id)}><Folder />查看分组资料</a></DropdownMenuItem>
                  <DropdownMenuItem onSelect={() => onSelectGroup?.(group.id)}><Folder />设为提问保存位置</DropdownMenuItem>
                  <DropdownMenuItem onSelect={() => openEditor({ kind: "group", group })}><Pencil />重命名</DropdownMenuItem>
                  <DropdownMenuItem variant="destructive" onSelect={() => openEditor({ kind: "delete-group", group })}><Trash2 />删除分组</DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          })}
          {groups.length > 5 && <button type="button" className={cn("flex items-center gap-2 px-2.5 py-1.5 text-label text-muted-foreground hover:text-foreground", focusRing)} onClick={() => setAllGroups(!allGroups)} aria-expanded={allGroups}><ChevronDown size={13} strokeWidth={1.8} aria-hidden="true" className={cn("transition-transform", allGroups && "rotate-180")} />{allGroups ? "收起分组" : `其余 ${groups.length - 5} 个分组`}</button>}
          {groups.length > 0 && <button type="button" className={cn("flex h-9 items-center gap-3 rounded-xl px-2.5 text-body2", focusRing, filter === "ungrouped" ? "bg-sidebar-accent text-sidebar-accent-foreground" : "text-foreground/75 hover:bg-sidebar-accent/60 hover:text-foreground")} onClick={() => selectView("ungrouped")}><Folder size={16} strokeWidth={1.8} aria-hidden="true" /><span className="flex-1 text-start">未分组</span><span className="text-label text-faint-foreground tabular-nums">{visits.filter(v => !v.groupId && !v.archivedAt).length}</span></button>}
          {!groups.length && <button type="button" disabled={!state.ready} className={cn("flex items-center gap-2 px-2.5 py-1.5 text-label text-muted-foreground hover:text-foreground disabled:opacity-50", focusRing)} onClick={() => openEditor({ kind: "group" })}><Plus size={13} strokeWidth={1.8} aria-hidden="true" />新建第一个分组</button>}
        </nav>
        {activeGroup && <a className={cn("mb-4 flex items-center gap-2.5 rounded-xl border border-border bg-card px-3 py-2 text-body2 text-foreground/75 hover:bg-accent hover:text-foreground dark:bg-input", focusRing)} href={researchGroupHref(activeGroup.id)}><Folder size={15} strokeWidth={1.8} aria-hidden="true" /><span className="flex-1 truncate">分组资料 · {state.workspace.materials?.filter(m => m.groupId === activeGroup.id).length ?? 0} 项</span><ChevronRight size={14} strokeWidth={1.8} aria-hidden="true" /></a>}

        <div className={sectionHeaderClass}><span className="min-w-0 truncate normal-case tracking-normal" title={label}>{label}</span><span className="shrink-0 font-normal tabular-nums">{filtered.length} 条</span></div>
        {!state.ready ? <div className="flex flex-col items-center gap-2 px-2.5 py-8 text-center text-body2 text-muted-foreground">{state.status === "loading" ? <><LoaderCircle size={18} strokeWidth={1.8} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /><p>正在读取历史…</p></> : <><Cloud size={18} strokeWidth={1.8} aria-hidden="true" /><p>暂时无法读取账号历史</p></>}</div>
          : filtered.length === 0 ? <div className="flex flex-col items-center gap-2 px-2.5 py-8 text-center text-body2 text-muted-foreground">
            <p>{search ? "没有找到相关研究" : filter === "archived" ? "还没有归档的研究" : filter === "favorites" ? "还没有收藏的研究" : filter === "recent" ? "还没有研究记录" : "这个分组还没有研究"}</p>
            {search && scope === "group" && <><small className="text-label text-faint-foreground">当前只搜索“{activeGroup?.name}”</small><Button variant="link" size="sm" className="h-auto p-0 text-foreground" onClick={() => setScope("all")}>在全部研究中搜索</Button></>}
            {search && <Button variant="link" size="sm" className="h-auto p-0 text-foreground" onClick={() => { setSearch(""); setScope("all"); setFilter("recent") }}>查看全部记录</Button>}
          </div>
          : dateSections.map(section => {
            const entries = filtered.filter(v => researchDateSection(v.updatedAt) === section)
            if (!entries.length) return null
            return <section key={section} aria-label={section} className="mt-2 flex flex-col gap-0.5">
              <h3 className="px-2.5 pb-1 pt-2 text-label font-normal text-faint-foreground">{section}</h3>
              {entries.map(visit => <VisitRow key={visit.id} visit={visit} selected={visit.id === activeVisitId} onResume={() => onResume(visit)}>
                <DropdownMenuItem onSelect={() => openEditor({ kind: "visit", visit })}><Pencil />重命名</DropdownMenuItem>
                <DropdownMenuItem onSelect={() => openEditor({ kind: "move", visit })}><Folder />移动到分组</DropdownMenuItem>
                <DropdownMenuItem onSelect={() => updateResearchVisit(visit.id, { isFavorite: !visit.isFavorite })}><Star />{visit.isFavorite ? "取消收藏" : "收藏"}</DropdownMenuItem>
                {visit.activity && <DropdownMenuItem asChild><a href={visit.href}><Search />返回检索结果</a></DropdownMenuItem>}
                <DropdownMenuItem onSelect={() => updateResearchVisit(visit.id, { archivedAt: visit.archivedAt ? null : Date.now() })}><Archive />{visit.archivedAt ? "恢复到最近研究" : "归档研究"}</DropdownMenuItem>
                <DropdownMenuItem variant="destructive" onSelect={() => { if (removeResearchVisit(visit.id)) setUndo({ id: visit.id, title: visit.title }) }}><Trash2 />删除</DropdownMenuItem>
              </VisitRow>)}
            </section>
          })}
      </div>}

      <div className={cn("flex shrink-0 flex-col gap-2 border-t border-sidebar-border py-3", collapsed ? "items-center px-0" : "px-2.5")}>
        {!collapsed && guestCount > 0 && state.ready && <div className="rounded-xl border border-border bg-card px-3 py-2.5 text-label text-muted-foreground dark:bg-input">
          <span>本机还有 {guestCount} 条研究</span>
          <div className="mt-1.5 flex gap-3">
            <Button variant="link" size="xs" className="h-auto p-0 text-foreground" onClick={() => { if (importGuestHistory()) { setImportHidden(true); setNotice("本机记录已合并，原本机副本保留。") } }}>合并到账号</Button>
            <Button variant="text" size="xs" className="h-auto p-0" onClick={() => setImportHidden(true)}>暂不合并</Button>
          </div>
        </div>}
        {!collapsed && (state.status === "error" || state.status === "conflict") && <div className="rounded-xl bg-destructive/10 px-3 py-2.5 text-label text-destructive" role="alert">
          <p>{state.message}</p>
          <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
            {state.owner && <>
              {account && <button type="button" className={cn("underline underline-offset-2", focusRing)} onClick={event => account.openLogin(event.currentTarget)}>重新登录</button>}
              <button type="button" className={cn("inline-flex items-center gap-1 underline underline-offset-2", focusRing)} onClick={() => window.dispatchEvent(new Event(retryHistoryEvent))}><RotateCcw size={12} strokeWidth={1.8} aria-hidden="true" />重试</button>
            </>}
            {state.ready && <button type="button" className={cn("inline-flex items-center gap-1 underline underline-offset-2", focusRing)} onClick={exportDraft}><Download size={12} strokeWidth={1.8} aria-hidden="true" />导出备份</button>}
            {state.status === "conflict" && <button type="button" className={cn("underline underline-offset-2", focusRing)} onClick={() => openEditor({ kind: "reload" })}>加载最新记录</button>}
          </div>
        </div>}
        <div className={cn("flex items-center gap-2 text-label text-faint-foreground", collapsed ? "justify-center" : "px-1")} title={state.owner ? "历史保存到当前账号" : "历史仅保存在当前浏览器"}>
          {state.status === "saving" || state.status === "loading" ? <LoaderCircle size={14} strokeWidth={1.8} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : state.owner ? <Cloud size={14} strokeWidth={1.8} aria-hidden="true" /> : <Laptop size={14} strokeWidth={1.8} aria-hidden="true" />}
          {!collapsed && <span>{saveStatusLabel(state.status)}</span>}
        </div>
        {account?.session ? <div className={cn(collapsed && "flex justify-center")}><PortalAccountControl compact={collapsed} side="top" /></div>
          : account && <button type="button" aria-label="登录同步研究" title={collapsed ? "登录同步研究" : undefined} onClick={event => account.openLogin(event.currentTarget)} className={cn("flex items-center gap-2.5 rounded-xl text-start transition-colors hover:bg-sidebar-accent/60", focusRing, collapsed ? "p-1" : "w-full px-1.5 py-1.5")}>
            <span className="grid size-9 shrink-0 place-items-center rounded-full bg-muted text-muted-foreground ring-1 ring-foreground/10"><UserRound size={16} strokeWidth={1.8} aria-hidden="true" /></span>
            {!collapsed && <><span className="min-w-0 flex-1"><span className="block truncate text-body2 font-medium text-foreground">登录同步研究</span><span className="block truncate text-label text-muted-foreground">在不同设备继续你的研究</span></span><ChevronRight size={14} strokeWidth={1.8} aria-hidden="true" className="text-faint-foreground" /></>}
          </button>}
      </div>
    </aside>
    {undo && <SidebarToast>
      <span className="min-w-0 truncate">已删除“{undo.title}”</span>
      <Button variant="ghost" size="sm" onClick={() => { if (restoreResearchVisit(undo.id)) setUndo(null) }}><RotateCcw />撤销</Button>
      <Button variant="ghost" size="icon-sm" aria-label="关闭删除提示" onClick={() => setUndo(null)}><X /></Button>
    </SidebarToast>}
    {notice && <SidebarToast>
      <Check size={16} strokeWidth={1.8} aria-hidden="true" className="shrink-0 text-success" />
      <span className="min-w-0 truncate">{notice}</span>
      <Button variant="ghost" size="icon-sm" aria-label="关闭提示" onClick={() => setNotice("")}><X /></Button>
    </SidebarToast>}
    <Dialog open={Boolean(editor)} onOpenChange={open => { if (!open) setEditor(null) }}>
      <DialogContent className="sm:max-w-[440px]">
        <DialogTitle className="pr-8">{editor?.kind === "visit" ? "重命名研究" : editor?.kind === "group" ? editor.group ? "重命名分组" : "新建分组" : editor?.kind === "move" ? "移动到分组" : editor?.kind === "delete-group" ? "删除分组" : "加载最新记录"}</DialogTitle>
        <DialogDescription className="break-words">{editor?.kind === "delete-group" ? `“${editor.group.name}”内的研究将移到未分组，不会被删除。` : editor?.kind === "reload" ? "将放弃此处尚未同步的修改并读取账号最新记录。建议先导出备份。" : editor?.kind === "move" ? editor.visit.title : ""}</DialogDescription>
        <form onSubmit={submitEditor} className="grid gap-4">
          {(editor?.kind === "visit" || editor?.kind === "group") && <label className="grid gap-2 text-body2 font-medium">{editor.kind === "visit" ? "研究标题" : "分组名称"}<Input autoFocus maxLength={editor.kind === "visit" ? 120 : 60} value={value} onChange={event => { setValue(event.target.value); setEditorError("") }} required /></label>}
          {editor?.kind === "move" && <label className="grid gap-2 text-body2 font-medium">目标分组<select className={inputClassName} value={value} onChange={event => setValue(event.target.value)}><option value="">未分组</option>{groups.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}</select></label>}
          {editorError && <p role="alert" className="text-body2 text-destructive">{editorError}</p>}
          <DialogFooter>
            <DialogClose asChild><Button type="button" variant="outline">取消</Button></DialogClose>
            <Button type="submit" variant={editor?.kind === "delete-group" || editor?.kind === "reload" ? "destructive" : "default"}>{editor?.kind === "delete-group" ? "删除分组" : editor?.kind === "reload" ? "放弃修改并加载" : "保存"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  </>
}

function SidebarToast({ children }: { children: ReactNode }) {
  return <div role="status" className="fixed bottom-6 left-1/2 z-[70] flex max-w-[600px] -translate-x-1/2 items-center gap-2 rounded-xl bg-popover py-2 pe-2 ps-4 text-body2 text-popover-foreground shadow-md ring-1 ring-foreground/10">{children}</div>
}

/** One research conversation in the history list, laid out like a rakazo roster row. */
function VisitRow({ visit, selected, onResume, children }: { visit: ResearchVisit; selected: boolean; onResume: () => void; children: ReactNode }) {
  const Icon = moduleIcons[visit.module]
  const preview = visitPreview(visit)
  return <div className={cn(rowClass, selected && "bg-sidebar-accent hover:bg-sidebar-accent")}>
    <button type="button" className={cn("flex min-w-0 flex-1 gap-3 rounded-xl px-2.5 py-[10px] text-start", focusRing)} aria-label={`继续研究：${visit.title}`} aria-current={selected ? "page" : undefined} title={visit.question} onClick={onResume}>
      <span className="grid size-9 shrink-0 place-items-center rounded-full bg-muted text-muted-foreground ring-1 ring-foreground/10"><Icon size={16} strokeWidth={1.8} aria-hidden="true" /></span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center justify-between gap-1.5">
          <span className="min-w-0 truncate text-body2 font-medium text-foreground">{visit.title}</span>
          <span className="flex shrink-0 items-center gap-1 text-label text-faint-foreground tabular-nums transition-opacity group-hover/row:opacity-0 group-focus-within/row:opacity-0">
            {visit.isFavorite && <Star size={11} strokeWidth={1.8} fill="currentColor" aria-label="已收藏" />}
            <time dateTime={new Date(visit.updatedAt).toISOString()}>{visitTime(visit)}</time>
          </span>
        </span>
        <span className="mt-0.5 line-clamp-2 text-label text-muted-foreground">{visit.archivedAt ? "已归档" : moduleInfo[visit.module].name}{preview && ` · ${preview}`}</span>
      </span>
    </button>
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger className={rowMenuClass} aria-label={`研究操作：${visit.title}`}><MoreHorizontal {...iconProps} /></DropdownMenuTrigger>
      <DropdownMenuContent className="min-w-44" side="right" align="start">{children}</DropdownMenuContent>
    </DropdownMenu>
  </div>
}
