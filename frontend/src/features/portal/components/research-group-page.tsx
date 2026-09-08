"use client"

import { useState, type FormEvent } from "react"
import * as Dialog from "@radix-ui/react-dialog"
import { useRouter } from "next/navigation"
import { ArrowLeft, ArrowUpRight, BookOpen, Check, ClipboardCheck, FileText, Folder, GitCompareArrows, Github, Pencil, Plus, Quote, Search, Trash2, X } from "lucide-react"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import { historyState, removeResearchMaterial, saveResearchMaterial } from "@/lib/research/history"
import { materialKindLabel, prepareMaterialReport, reportDraftHref } from "@/lib/research/materials"
import type { ResearchMaterial } from "@/lib/research/workspace-items"
import styles from "./research-workspace.module.css"

const icons = { paper: BookOpen, project: Github, discussion: Quote, note: FileText, pdf: FileText }

export function ResearchGroupPage({ groupId }: { groupId: string | null }) {
  const state = useOwnedResearchWorkspace()
  if (!state.ready) return <div className={styles.page}><main className={styles.main}><a href="/design-demo">返回首页</a><p className={styles.empty} role="status">{state.status === "error" ? "研究资料暂时无法读取，请重新登录或稍后重试。" : "正在读取研究资料…"}</p></main></div>
  return <GroupEditor key={state.owner ?? "guest"} groupId={groupId} state={state} />
}

function GroupEditor({ groupId, state }: { groupId: string | null; state: ReturnType<typeof useOwnedResearchWorkspace> }) {
  const router = useRouter()
  const [search, setSearch] = useState("")
  const [kind, setKind] = useState("all")
  const [selected, setSelected] = useState<string[]>([])
  const [editor, setEditor] = useState<ResearchMaterial | null>(null)
  const [deleting, setDeleting] = useState<ResearchMaterial | null>(null)
  const [compare, setCompare] = useState(false)
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  const group = state.workspace.groups.find(g => g.id === groupId)
  const title = groupId ? group?.name : "未分组资料"
  const materials = (state.workspace.materials ?? []).filter(m => m.groupId === groupId).sort((a, b) => b.updatedAt - a.updatedAt)
  const visible = materials.filter(m => (kind === "all" || m.kind === kind) && `${m.title} ${m.notes}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()))
  const chosen = materials.filter(m => selected.includes(m.id))
  const drafts = (state.workspace.reportDrafts ?? []).filter(d => (d.groupId ?? null) === groupId)
  const owned = () => state.owner === historyState().owner && historyState().ready

  function edit(material?: ResearchMaterial) {
    const now = Date.now()
    setEditor(material ?? { id: crypto.randomUUID(), groupId, kind: "note", title: "", url: "", notes: "", createdAt: now, updatedAt: now })
    setError("")
  }
  function save(event: FormEvent) {
    event.preventDefault()
    if (!editor || !owned()) return
    if (!saveResearchMaterial({ ...editor, title: editor.title.trim(), updatedAt: Date.now() }, state.owner)) { setError(historyState().message || "资料未保存，请检查标题、来源链接和内容长度。"); return }
    setEditor(null); setError(""); setNotice("资料已保存。")
  }
  function report() {
    const draft = prepareMaterialReport(title || "研究报告", groupId, chosen.map(m => m.id), state.owner)
    if (!draft) { setError("无法准备报告，请检查所选资料或账号状态。"); return }
    router.push(reportDraftHref(draft))
  }
  function useContext() {
    const params = new URLSearchParams()
    if (groupId) params.set("researchGroup", groupId)
    chosen.forEach(m => params.append("material", m.id))
    router.push(`/design-demo?${params}`)
  }
  if (!title) return <div className={styles.page}><main className={styles.main}><a href="/design-demo">返回首页</a><p className={styles.empty}>当前账号下找不到这个分组。</p></main></div>

  return <div className={styles.page}>
    <header className={styles.header}><a href="/design-demo"><ArrowLeft size={19} />我的研究</a><small>{state.owner ? state.status === "synced" ? "已同步到账号" : state.status === "saving" ? "正在同步…" : "尚未同步" : "资料保存在本机"}</small></header>
    <main className={styles.main}>
      <div className={styles.titleRow}><div><h1>{title}</h1><p>{materials.length} 项资料 · {drafts.length} 份报告准备稿</p></div><button type="button" className={styles.primary} onClick={() => edit()}><Plus size={17} />添加资料</button></div>
      <div className={styles.toolbar}><label className={styles.search}><Search size={17} /><input aria-label="搜索分组资料" placeholder="搜索标题、笔记" value={search} onChange={e => setSearch(e.target.value)} /></label><select className={styles.select} aria-label="资料类型" value={kind} onChange={e => setKind(e.target.value)}><option value="all">全部类型</option>{Object.entries(materialKindLabel).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div>
      <div className={styles.selection}><label className={styles.actions}><input type="checkbox" aria-label="全选当前资料" checked={visible.length > 0 && visible.every(m => selected.includes(m.id))} onChange={e => setSelected(e.target.checked ? [...new Set([...selected, ...visible.map(m => m.id)])] : selected.filter(id => !visible.some(m => m.id === id)))} />已选 {chosen.length} 项</label><button className={styles.button} disabled={chosen.length < 2 || chosen.length > 4} onClick={() => setCompare(true)} title="选择 2 至 4 项资料进行对比"><GitCompareArrows size={16} />对比资料</button><button className={styles.button} disabled={!chosen.length} onClick={useContext}><Folder size={16} />带入提问</button><button className={styles.button} disabled={!chosen.length || chosen.length > 100} onClick={report}><ClipboardCheck size={16} />准备报告</button></div>
      {error && !editor && <p role="alert" className={styles.error}>{error}</p>}{state.message && <p role="alert" className={styles.error}>{state.message}</p>}{notice && <p role="status" className={styles.notice}>{notice}</p>}
      {visible.length ? <ul className={styles.list}>{visible.map(m => { const Icon = icons[m.kind]; return <li className={styles.row} key={m.id}><input type="checkbox" aria-label={`选择资料：${m.title}`} checked={selected.includes(m.id)} onChange={e => setSelected(e.target.checked ? [...selected, m.id] : selected.filter(id => id !== m.id))} /><span className={styles.rowIcon}><Icon size={21} /></span><div className={styles.rowContent}><h3>{m.title}</h3><small>{materialKindLabel[m.kind]} · {new Date(m.updatedAt).toLocaleDateString("zh-CN")}</small>{m.notes && <p>{m.notes}</p>}<div className={styles.actions}>{m.url && <a href={m.url} target="_blank" rel="noreferrer">查看来源<ArrowUpRight size={14} /></a>}{m.readerHref && <a href={m.readerHref}><BookOpen size={14} />继续阅读</a>}</div></div><button className={styles.iconButton} aria-label={`编辑资料：${m.title}`} title="编辑资料" onClick={() => edit(m)}><Pencil size={17} /></button><button className={styles.iconButton} aria-label={`移除资料：${m.title}`} title="移除资料" onClick={() => setDeleting(m)}><Trash2 size={17} /></button></li> })}</ul> : <div className={styles.empty}>{search || kind !== "all" ? "没有匹配的资料" : "还没有资料"}{search && <p><button className={styles.button} onClick={() => { setSearch(""); setKind("all") }}>查看全部资料</button></p>}</div>}
      <section className={styles.drafts}><h2>报告准备稿</h2>{drafts.length ? drafts.map(d => <a key={d.id} href={reportDraftHref(d)}><span>{d.title}</span><span>{new Date(d.updatedAt).toLocaleDateString("zh-CN")}</span></a>) : <p className={styles.description}>暂无报告准备稿</p>}</section>
    </main>
    <Dialog.Root open={Boolean(editor)} onOpenChange={open => { if (!open) setEditor(null) }}><Dialog.Portal><Dialog.Overlay className={styles.overlay} /><Dialog.Content className={styles.dialog}><Dialog.Title>{editor && materials.some(m => m.id === editor.id) ? "编辑资料" : "添加资料"}</Dialog.Title><Dialog.Description className={styles.description}>{title}</Dialog.Description>{editor && <form onSubmit={save}><label className={styles.field}>类型<select value={editor.kind} onChange={e => setEditor({ ...editor, kind: e.target.value as ResearchMaterial["kind"] })}>{Object.entries(materialKindLabel).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label className={styles.field}>标题<input autoFocus required maxLength={500} value={editor.title} onChange={e => setEditor({ ...editor, title: e.target.value })} /></label><label className={styles.field}>来源链接<input type="url" required={editor.kind !== "note" && !editor.readerHref} maxLength={2000} value={editor.url} placeholder="https://" onChange={e => setEditor({ ...editor, url: e.target.value })} /></label><label className={styles.field}>笔记<textarea maxLength={10000} value={editor.notes} onChange={e => setEditor({ ...editor, notes: e.target.value })} /></label><label className={styles.field}>分组<select value={editor.groupId ?? ""} onChange={e => setEditor({ ...editor, groupId: e.target.value || null })}><option value="">未分组</option>{state.workspace.groups.map(g => <option value={g.id} key={g.id}>{g.name}</option>)}</select></label>{error && <p className={styles.error} role="alert">{error}</p>}<div className={styles.dialogActions}><Dialog.Close className={styles.button} type="button">取消</Dialog.Close><button type="submit" className={styles.primary}><Check size={16} />保存</button></div></form>}<Dialog.Close className={`${styles.iconButton} ${styles.close}`} aria-label="关闭资料编辑"><X size={18} /></Dialog.Close></Dialog.Content></Dialog.Portal></Dialog.Root>
    <Dialog.Root open={Boolean(deleting)} onOpenChange={open => { if (!open) setDeleting(null) }}><Dialog.Portal><Dialog.Overlay className={styles.overlay} /><Dialog.Content className={styles.dialog}><Dialog.Title>移除资料</Dialog.Title><Dialog.Description className={styles.description}>移除“{deleting?.title}”？已经保存在报告中的资料副本不会受影响。</Dialog.Description><div className={styles.dialogActions}><Dialog.Close className={styles.button}>取消</Dialog.Close><button className={styles.primary} onClick={() => { if (deleting && owned() && removeResearchMaterial(deleting.id, state.owner)) { setSelected(selected.filter(id => id !== deleting.id)); setDeleting(null); setNotice("资料已移除。") } }}>移除</button></div></Dialog.Content></Dialog.Portal></Dialog.Root>
    <Dialog.Root open={compare} onOpenChange={setCompare}><Dialog.Portal><Dialog.Overlay className={styles.overlay} /><Dialog.Content className={`${styles.dialog} ${styles.comparison}`}><Dialog.Title>资料对比</Dialog.Title><Dialog.Description className={styles.description}>已保存的来源与笔记</Dialog.Description><div className={styles.tableWrap}><table><thead><tr><th>对比项</th>{chosen.map(m => <th key={m.id}>{m.title}</th>)}</tr></thead><tbody><tr><th>类型</th>{chosen.map(m => <td key={m.id}>{materialKindLabel[m.kind]}</td>)}</tr><tr><th>来源</th>{chosen.map(m => <td key={m.id}>{m.url ? <a href={m.url} target="_blank" rel="noreferrer">{m.url}</a> : "未提供"}</td>)}</tr><tr><th>资料标识</th>{chosen.map(m => <td key={m.id}>{m.referenceId || "未记录"}</td>)}</tr><tr><th>研究笔记</th>{chosen.map(m => <td key={m.id}>{m.notes || "尚未记录"}</td>)}</tr></tbody></table></div><div className={styles.dialogActions}><button className={styles.primary} onClick={report}><ClipboardCheck size={16} />带入报告准备稿</button></div><Dialog.Close className={`${styles.iconButton} ${styles.close}`} aria-label="关闭资料对比"><X size={18} /></Dialog.Close></Dialog.Content></Dialog.Portal></Dialog.Root>
  </div>
}
