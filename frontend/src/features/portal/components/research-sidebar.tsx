"use client"

import { useEffect, useRef, useState, type FormEvent } from "react"
import * as Dialog from "@radix-ui/react-dialog"
import { Archive, BookOpen, Check, ChevronDown, ChevronRight, Cloud, Download, Folder, FolderPlus, Github, History, Laptop, LoaderCircle, MoreHorizontal, PanelLeftClose, PanelLeftOpen, Pencil, Plus, Quote, RotateCcw, Search, Star, Trash2, X, ClipboardCheck, UserRound } from "lucide-react"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { usePortalAccount } from "@/components/auth/portal-account-provider"
import { PortalAccountControl } from "@/components/auth/portal-account-control"
import { historyState, importGuestHistory, readGuestHistory, removeResearchGroup, removeResearchVisit, restoreResearchVisit, saveResearchGroup, updateResearchVisit, type ResearchGroup, type ResearchVisit } from "@/lib/research/history"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import { reloadHistoryEvent, retryHistoryEvent } from "@/lib/research/history-sync"
import { moduleInfo } from "@/lib/research/entry"
import styles from "./research-sidebar.module.css"

const moduleIcons = { papers: BookOpen, projects: Github, community: Quote, reports: ClipboardCheck }
type Editor = { kind: "visit"; visit: ResearchVisit } | { kind: "group"; group?: ResearchGroup } | { kind: "move"; visit: ResearchVisit } | { kind: "delete-group"; group: ResearchGroup } | { kind: "reload" }
export function researchDateSection(timestamp: number, now = new Date()) {
  const date = new Date(timestamp)
  const day = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()) - Date.UTC(date.getFullYear(), date.getMonth(), date.getDate())
  return day <= 0 ? "今天" : day === 86400000 ? "昨天" : day < 7 * 86400000 ? "过去 7 天" : "更早"
}

export function ResearchSidebar({ collapsed, onToggle, onNew, onResume, onSelectGroup, busy = false }: { busy?: boolean; collapsed: boolean; onToggle: () => void; onNew: () => void; onResume: (visit: ResearchVisit) => void; onSelectGroup?: (id: string | null) => void }) {
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
  const editorTitle = editor?.kind === "visit" ? "重命名研究" : editor?.kind === "group" ? editor.group ? "重命名分组" : "新建分组" : editor?.kind === "move" ? "移动到分组" : editor?.kind === "delete-group" ? "删除分组" : "加载最新记录"
  const selectView = (next: string) => { setFilter(next); if (collapsed) onToggle() }

  return <>
    <aside className={`${styles.sidebar} ${collapsed ? styles.collapsed : ""}`} aria-label="研究历史侧栏">
      <div className={styles.top}>
        {!collapsed && <span className={styles.heading}>我的研究</span>}
        <button type="button" className={styles.iconButton} onClick={onToggle} aria-label={collapsed ? "展开研究侧栏" : "收起研究侧栏"} title={collapsed ? "展开研究侧栏" : "收起研究侧栏"}>{collapsed ? <PanelLeftOpen size={19} /> : <PanelLeftClose size={19} />}</button>
      </div>
      <div className={styles.tools}>
        <button type="button" className={styles.newResearch} disabled={busy} onClick={() => { setFilter("recent"); setSearch(""); onNew() }} title="新研究"><Plus size={20} />{!collapsed && <span>新研究</span>}</button>
        {collapsed ? <button type="button" className={styles.iconButton} title="搜索历史" aria-label="搜索历史" onClick={() => { onToggle(); setTimeout(() => searchRef.current?.focus(), 0) }}><Search size={19} /></button> : <div className={styles.search}><Search size={16} /><input ref={searchRef} type="search" aria-label="搜索研究历史" placeholder="搜索历史" value={search} onChange={event => setSearch(event.target.value)} />{search && <button type="button" aria-label="清空历史搜索" onClick={() => setSearch("")}><X size={14} /></button>}</div>}
        {!collapsed && <label className={styles.searchScope}>搜索范围<select aria-label="历史搜索范围" value={scope} onChange={event => setScope(event.target.value as "all" | "group")}><option value="all">全部研究</option>{activeGroup && <option value="group">当前分组</option>}</select></label>}
        <button type="button" className={`${styles.navItem} ${filter === "recent" ? styles.active : ""}`} aria-pressed={filter === "recent"} title="最近研究" onClick={() => selectView("recent")}><History size={18} />{!collapsed && <><span>最近研究</span><small>{visits.filter(v => !v.archivedAt).length}</small></>}</button>
        <button type="button" className={`${styles.navItem} ${filter === "favorites" ? styles.active : ""}`} aria-pressed={filter === "favorites"} title="已收藏" onClick={() => selectView("favorites")}><Star size={18} />{!collapsed && <><span>已收藏</span><small>{visits.filter(v => v.isFavorite).length}</small></>}</button>
        <button type="button" className={`${styles.navItem} ${filter === "archived" ? styles.active : ""}`} aria-pressed={filter === "archived"} title="已归档" onClick={() => selectView("archived")}><Archive size={18} />{!collapsed && <><span>已归档</span><small>{visits.filter(v => v.archivedAt).length}</small></>}</button>
      </div>
      {!collapsed && <div className={styles.scrollArea}>
        {favorites.length > 0 && <div className={styles.favorites}><button type="button" className={styles.showMore} onClick={() => setShowFavorites(!showFavorites)} aria-expanded={showFavorites}><ChevronDown size={14} />{showFavorites ? "收起收藏快捷入口" : "展开收藏快捷入口"}</button>{showFavorites && favorites.slice(0, 5).map(v => <button key={v.id} type="button" className={styles.favoriteLink} onClick={() => onResume(v)} title={v.title}>★ {v.title}</button>)}{showFavorites && favorites.length > 5 && <button className={styles.showMore} onClick={() => selectView("favorites")}>查看全部 {favorites.length} 项收藏</button>}</div>}
        <div className={styles.sectionHeading}><span>我的分组</span><button type="button" className={styles.iconButton} title="新建分组" aria-label="新建分组" disabled={!state.ready} onClick={() => openEditor({ kind: "group" })}><FolderPlus size={17} /></button></div>
        <nav aria-label="研究分组" className={styles.groups}>
          {(allGroups ? groups : groups.slice(0, 5)).map(group => <div className={`${styles.groupRow} ${filter === group.id ? styles.active : ""}`} key={group.id}>
            <button type="button" className={styles.groupLink} onClick={() => selectView(group.id)} aria-current={filter === group.id ? "page" : undefined} title={group.name}><Folder size={17} /><span>{group.name}</span><small>{visits.filter(v => v.groupId === group.id && !v.archivedAt).length}</small></button>
            <DropdownMenu modal={false}><DropdownMenuTrigger className={styles.rowMenu} aria-label={`分组操作：${group.name}`}><MoreHorizontal size={17} /></DropdownMenuTrigger><DropdownMenuContent className={styles.menu} side="right" align="start"><DropdownMenuItem asChild><a href={`/design-demo/groups/${encodeURIComponent(group.id)}`}><Folder size={15} />查看分组资料</a></DropdownMenuItem><DropdownMenuItem onSelect={() => onSelectGroup?.(group.id)}><Folder size={15} />设为提问保存位置</DropdownMenuItem><DropdownMenuItem onSelect={() => openEditor({ kind: "group", group })}><Pencil size={15} />重命名</DropdownMenuItem><DropdownMenuItem onSelect={() => openEditor({ kind: "delete-group", group })}><Trash2 size={15} />删除分组</DropdownMenuItem></DropdownMenuContent></DropdownMenu>
          </div>)}
          {groups.length > 5 && <button type="button" className={styles.showMore} onClick={() => setAllGroups(!allGroups)} aria-expanded={allGroups}><ChevronDown size={14} />{allGroups ? "收起分组" : `其余 ${groups.length - 5} 个分组`}</button>}
          {groups.length > 0 && <button type="button" className={`${styles.navItem} ${filter === "ungrouped" ? styles.active : ""}`} onClick={() => selectView("ungrouped")}><Folder size={16} /><span>未分组</span><small>{visits.filter(v => !v.groupId && !v.archivedAt).length}</small></button>}
          {!groups.length && <button type="button" disabled={!state.ready} className={styles.createFirst} onClick={() => openEditor({ kind: "group" })}><Plus size={14} />新建第一个分组</button>}
        </nav>
        {activeGroup && <a className={styles.materialLink} href={`/design-demo/groups/${encodeURIComponent(activeGroup.id)}`}><Folder size={15} />分组资料 · {state.workspace.materials?.filter(m => m.groupId === activeGroup.id).length ?? 0} 项<ChevronRight size={14} /></a>}
        <div className={styles.historyHeading}><span title={label}>{label}</span><small>{filtered.length} 条</small></div>
        {!state.ready ? <div className={styles.empty}>{state.status === "loading" ? <><LoaderCircle className={styles.spinner} size={20} /><p>正在读取历史…</p></> : <><Cloud size={22} /><p>暂时无法读取账号历史</p></>}</div> : filtered.length === 0 ? <div className={styles.empty}><Search size={23} /><p>{search ? "没有找到相关研究" : filter === "archived" ? "还没有归档的研究" : filter === "favorites" ? "还没有收藏的研究" : filter === "recent" ? "还没有研究记录" : "这个分组还没有研究"}</p>{search && scope === "group" && <><small>当前只搜索“{activeGroup?.name}”</small><button type="button" onClick={() => setScope("all")}>在全部研究中搜索</button></>}{search && <button type="button" onClick={() => { setSearch(""); setScope("all"); setFilter("recent") }}>查看全部记录</button>}</div> : ["今天", "昨天", "过去 7 天", "更早"].map(section => {
          const entries = filtered.filter(v => researchDateSection(v.updatedAt) === section)
          if (!entries.length) return null
          return <section key={section} aria-label={section} className={styles.dateSection}><h3>{section}</h3>{entries.map(visit => { const Icon = moduleIcons[visit.module]; return <div key={visit.id} className={styles.visit}>
            <button type="button" className={styles.visitLink} aria-label={`继续研究：${visit.title}`} title={visit.question} onClick={() => onResume(visit)}><span className={styles.visitTitle}>{visit.title}</span>{visit.activity && <span className={styles.activity} title={visit.activity.title}>{visit.activity.kind === "reader" ? `阅读：${visit.activity.sectionTitle || visit.activity.title}` : `草稿：${visit.activity.title}`}</span>}<span className={styles.visitMeta}><Icon size={13} /><span>{visit.archivedAt ? "已归档" : moduleInfo[visit.module].name}</span>{visit.isFavorite && <Star size={12} fill="currentColor" />}<time dateTime={new Date(visit.updatedAt).toISOString()}>{new Date(visit.updatedAt).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" })}</time></span></button>
            <DropdownMenu modal={false}><DropdownMenuTrigger className={styles.rowMenu} aria-label={`研究操作：${visit.title}`}><MoreHorizontal size={18} /></DropdownMenuTrigger><DropdownMenuContent className={styles.menu} side="right" align="start"><DropdownMenuItem onSelect={() => openEditor({ kind: "visit", visit })}><Pencil size={15} />重命名</DropdownMenuItem><DropdownMenuItem onSelect={() => openEditor({ kind: "move", visit })}><Folder size={15} />移动到分组</DropdownMenuItem><DropdownMenuItem onSelect={() => updateResearchVisit(visit.id, { isFavorite: !visit.isFavorite })}><Star size={15} />{visit.isFavorite ? "取消收藏" : "收藏"}</DropdownMenuItem>{visit.activity && <DropdownMenuItem asChild><a href={visit.href}><Search size={15} />返回检索结果</a></DropdownMenuItem>}<DropdownMenuItem onSelect={() => updateResearchVisit(visit.id, { archivedAt: visit.archivedAt ? null : Date.now() })}><Archive size={15} />{visit.archivedAt ? "恢复到最近研究" : "归档研究"}</DropdownMenuItem><DropdownMenuItem onSelect={() => { if (removeResearchVisit(visit.id)) setUndo({ id: visit.id, title: visit.title }) }}><Trash2 size={15} />删除</DropdownMenuItem></DropdownMenuContent></DropdownMenu>
          </div> })}</section>
        })}
      </div>}
      <div className={styles.bottom}>
        {!collapsed && guestCount > 0 && state.ready && <div className={styles.import}><span>本机还有 {guestCount} 条研究</span><div><button type="button" onClick={() => { if (importGuestHistory()) { setImportHidden(true); setNotice("本机记录已合并，原本机副本保留。") } }}>合并到账号</button><button type="button" onClick={() => setImportHidden(true)}>暂不合并</button></div></div>}
        {!collapsed && (state.status === "error" || state.status === "conflict") && <div className={styles.syncError} role="alert">
          <p>{state.message}</p>
          <div>
            {state.owner && <>
              {account && <button type="button" onClick={event => account.openLogin(event.currentTarget)}>重新登录</button>}
              <button type="button" onClick={() => window.dispatchEvent(new Event(retryHistoryEvent))}><RotateCcw size={13} />重试</button>
            </>}
            {state.ready && <button type="button" onClick={exportDraft}><Download size={13} />导出备份</button>}
            {state.status === "conflict" && <button type="button" onClick={() => openEditor({ kind: "reload" })}>加载最新记录</button>}
          </div>
        </div>}
        <div className={styles.saveState} title={state.owner ? "历史保存到当前账号" : "历史仅保存在当前浏览器"}>{state.status === "saving" || state.status === "loading" ? <LoaderCircle size={16} className={styles.spinner} /> : state.owner ? <Cloud size={16} /> : <Laptop size={16} />}{!collapsed && <span>{state.status === "saving" ? "正在同步…" : state.status === "synced" ? "已同步到账号" : state.status === "loading" ? "正在加载…" : state.status === "error" || state.status === "conflict" ? "尚未保存" : "历史保存在本机"}</span>}</div>
        {account?.session ? <div className={styles.accountControl}><PortalAccountControl compact={collapsed} side="top" /></div> : account && <button className={styles.login} type="button" aria-label="登录同步研究" title={collapsed ? "登录同步研究" : undefined} onClick={event => account.openLogin(event.currentTarget)}>{collapsed ? <UserRound size={18} /> : <>登录同步研究<ChevronRight size={14} /></>}</button>}
      </div>
    </aside>
    {undo && <div role="status" className={styles.toast}><span>已删除“{undo.title}”</span><button type="button" onClick={() => { if (restoreResearchVisit(undo.id)) setUndo(null) }}><RotateCcw size={15} />撤销</button><button type="button" aria-label="关闭删除提示" onClick={() => setUndo(null)}><X size={15} /></button></div>}
    {notice && <div role="status" className={styles.toast}><Check size={17} /><span>{notice}</span><button type="button" aria-label="关闭提示" onClick={() => setNotice("")}><X size={15} /></button></div>}
    <Dialog.Root open={Boolean(editor)} onOpenChange={open => { if (!open) setEditor(null) }}><Dialog.Portal><Dialog.Overlay className={styles.overlay} /><Dialog.Content className={styles.dialog}>
      <Dialog.Title className={styles.dialogTitle}>{editorTitle}</Dialog.Title>
      <Dialog.Description className={styles.dialogDescription}>{editor?.kind === "delete-group" ? `“${editor.group.name}”内的研究将移到未分组，不会被删除。` : editor?.kind === "reload" ? "将放弃此处尚未同步的修改并读取账号最新记录。建议先导出备份。" : editor?.kind === "move" ? editor.visit.title : ""}</Dialog.Description>
      <form onSubmit={submitEditor}>
        {(editor?.kind === "visit" || editor?.kind === "group") && <label className={styles.field}>{editor.kind === "visit" ? "研究标题" : "分组名称"}<input autoFocus maxLength={editor.kind === "visit" ? 120 : 60} value={value} onChange={event => { setValue(event.target.value); setEditorError("") }} required /></label>}
        {editor?.kind === "move" && <label className={styles.field}>目标分组<select value={value} onChange={event => setValue(event.target.value)}><option value="">未分组</option>{groups.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}</select></label>}
        {editorError && <p role="alert" className={styles.formError}>{editorError}</p>}
        <div className={styles.dialogActions}><Dialog.Close type="button">取消</Dialog.Close><button type="submit" className={styles.confirm}>{editor?.kind === "delete-group" ? "删除分组" : editor?.kind === "reload" ? "放弃修改并加载" : "保存"}</button></div>
      </form><Dialog.Close className={styles.closeDialog} aria-label="关闭对话框"><X size={18} /></Dialog.Close>
    </Dialog.Content></Dialog.Portal></Dialog.Root>
  </>
}
