"use client"

import { useEffect, useRef, useState } from "react"
import { BookOpen, Check, Plus, Save, Trash2 } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { inputClassName } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import { researchQuestionHref } from "@/lib/research/entry"
import { deleteReportDraft, newReportDraft, readReportDrafts, readLegacyReportDrafts, importLegacyReportDrafts, reportDraftEvent, safeMaterialUrl, saveReportDraft, type ReportDraft, type ReportMaterial } from "../lib/report-drafts"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import { rememberResearchActivity, recordResearchVisit } from "@/lib/research/history"
import { selectedWorkspaceMaterials } from "@/lib/research/materials"

/** Multi-line fields reuse the input recipe with auto height. */
const fieldClass = cn(inputClassName, "h-auto py-2")
const statusClass = "text-body2 text-muted-foreground"

export function ReportPreparation({ question, draftId, onSaved }: { question: string; draftId?: string | null; onSaved: (draft: ReportDraft) => void }) {
  const state = useOwnedResearchWorkspace()
  if (!state.ready) return <p role="status" className={statusClass}>{state.status === "error" ? "暂时无法读取研究草稿，请重新登录或稍后重试。" : "正在读取研究草稿…"}</p>
  return <ReportPreparationEditor key={state.owner ?? "guest"} question={question} draftId={draftId} onSaved={onSaved} state={state} />
}

function ReportPreparationEditor({ question, draftId, onSaved, state }: { question: string; draftId?: string | null; onSaved: (draft: ReportDraft) => void; state: ReturnType<typeof useOwnedResearchWorkspace> }) {
  const [draft, setDraft] = useState<ReportDraft | null>(null)
  const [drafts, setDrafts] = useState<ReportDraft[]>([])
  const [dirty, setDirty] = useState(false)
  const [notice, setNotice] = useState("")
  const [error, setError] = useState("")
  const initialized = useRef("")
  useEffect(() => {
    const identity = JSON.stringify([question, draftId])
    if (initialized.current === identity) return
    initialized.current = identity
    const all = readReportDrafts()
    const found = draftId ? all.find(item => item.id === draftId) : undefined
    const params = new URLSearchParams(window.location.search)
    const groupId = state.workspace.groups.some(g => g.id === params.get("researchGroup")) ? params.get("researchGroup") : null
    const materials = selectedWorkspaceMaterials(window.location.search, state.workspace.materials ?? []).slice(0, 100).map(m => ({ id: m.id, title: m.title, url: m.url, notes: m.notes }))
    setDraft(found ?? { ...newReportDraft(question), groupId, materials })
    setDirty(false)
    setError(draftId && !found ? "当前账号下找不到这份草稿，可以重新整理并保存。" : "")
    setNotice("")
  }, [question, draftId, state.workspace.groups, state.workspace.materials])
  useEffect(() => {
    const update = () => setDrafts(readReportDrafts())
    update()
    window.addEventListener(reportDraftEvent, update)
    window.addEventListener("storage", update)
    return () => { window.removeEventListener(reportDraftEvent, update); window.removeEventListener("storage", update) }
  }, [])
  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = "" }
    window.addEventListener("beforeunload", warn)
    return () => window.removeEventListener("beforeunload", warn)
  }, [dirty])
  function edit(patch: Partial<ReportDraft>) { setDraft(current => current ? { ...current, ...patch } : current); setDirty(true); setNotice(""); setError("") }
  function editMaterial(id: string, patch: Partial<ReportMaterial>) { if (draft) edit({ materials: draft.materials.map(item => item.id === id ? { ...item, ...patch } : item) }) }
  function save() {
    if (!draft) return
    if (!draft.title.trim()) { setError("请先填写研究主题。"); return }
    if (draft.materials.some(item => !safeMaterialUrl(item.url))) { setError("资料链接请使用完整的 http:// 或 https:// 地址。"); return }
    const saved = { ...draft, title: draft.title.trim(), updatedAt: Date.now() }
    if (!saveReportDraft(saved, state.owner)) { setError("草稿未能保存，请检查账号状态或可用存储空间。当前内容仍保留在页面中。"); return }
    setDraft(saved); setDirty(false); setError(""); setNotice(state.owner ? "草稿修改已保留，正在同步到账号。" : "已保存到本机，可以随时继续编辑。"); onSaved(saved)
    const href = window.location.pathname + window.location.search
    const sessionId = new URLSearchParams(window.location.search).get("researchSession")
    if (sessionId) { recordResearchVisit(href); rememberResearchActivity(sessionId, { kind: "report", draftId: saved.id, title: saved.title.slice(0, 500), updatedAt: saved.updatedAt }, state.owner) }
  }
  function open(saved: ReportDraft) {
    if (dirty) { setError("请先保存当前修改，再打开其他草稿。"); return }
    setDraft(saved); setNotice(""); setError(""); onSaved(saved)
  }
  if (!draft) return <p role="status" className={statusClass}>正在打开报告准备页…</p>
  const legacy = readLegacyReportDrafts().filter(item => !drafts.some(d => d.id === item.id))
  return <section aria-label="报告准备" className="rounded-2xl border bg-card text-card-foreground">
    <div className="flex flex-wrap items-start justify-between gap-4 border-b px-4 py-4 md:px-6"><div><Badge variant={state.owner ? state.status === "synced" ? "success" : state.status === "saving" ? "muted" : "warning" : "muted"}>{state.owner ? state.status === "synced" ? "已同步到账号" : state.status === "saving" ? "正在同步…" : "账号草稿 · 尚未同步" : "本机草稿"}</Badge><h2 className="mt-2 text-heading3 font-semibold">准备你的研究报告</h2><p className="mt-1 text-body2 text-muted-foreground">先明确问题，收集资料，留下你的分析与思路。</p></div><Button type="button" onClick={save}><Save size={15} strokeWidth={1.8} />保存草稿</Button></div>
    <div className="p-4 md:p-6">
    {legacy.length > 0 && <div className="mb-6 flex flex-wrap items-center justify-between gap-3 rounded-xl border px-4 py-3 text-body2"><span>此浏览器有 {legacy.length} 份旧版草稿，导入后可在{state.owner ? "当前账号" : "本机工作区"}继续编辑。</span><Button type="button" variant="outline" size="sm" onClick={() => { if (!importLegacyReportDrafts(state.owner)) setError("旧版草稿导入失败，原始内容仍保留。"); else setNotice("旧版草稿已导入，原始备份仍保留。") }}>导入旧版草稿</Button></div>}
    <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_280px]">
      <div className="space-y-6">
        <label className="block space-y-2"><span className="text-body2 font-medium">保存到分组</span><select aria-label="保存到分组" className={cn(inputClassName, "cursor-pointer")} value={draft.groupId ?? ""} onChange={event => edit({ groupId: event.target.value || null })}><option value="">未分组</option>{state.workspace.groups.map(group => <option key={group.id} value={group.id}>{group.name}</option>)}</select></label>
        <label className="block space-y-2"><span className="text-body2 font-medium">研究主题</span><input aria-label="研究主题" className={inputClassName} maxLength={2000} value={draft.title} onChange={event => edit({ title: event.target.value })} placeholder="你想回答什么问题？" /></label>
        <label className="block space-y-2"><span className="text-body2 font-medium">研究范围</span><textarea aria-label="研究范围" className={fieldClass} rows={3} maxLength={10000} value={draft.scope} onChange={event => edit({ scope: event.target.value })} placeholder="关注哪些方法、时间范围或比较维度？" /></label>
        <div className="space-y-3"><div className="flex items-center justify-between"><h3 className="text-body2 font-medium">参考资料</h3><Button type="button" variant="ghost" size="sm" disabled={draft.materials.length >= 30} onClick={() => edit({ materials: [...draft.materials, { id: crypto.randomUUID(), title: "", url: "", notes: "" }] })}><Plus size={15} strokeWidth={1.8} />添加资料</Button></div>
          {!draft.materials.length && <p className="py-4 text-body2 text-muted-foreground">添加论文、项目或讨论链接，也可以直接记录资料摘要。</p>}
          {draft.materials.map((item, index) => <div key={item.id} className="space-y-3 rounded-xl border p-4"><div className="flex items-center justify-between"><span className="text-label text-muted-foreground">资料 {index + 1}</span><Button type="button" variant="ghost" size="icon-sm" aria-label={`移除资料 ${index + 1}`} onClick={() => edit({ materials: draft.materials.filter(material => material.id !== item.id) })} className="text-muted-foreground hover:text-destructive-text"><Trash2 size={15} strokeWidth={1.8} /></Button></div><input aria-label={`资料 ${index + 1} 标题`} className={inputClassName} maxLength={500} value={item.title} onChange={event => editMaterial(item.id, { title: event.target.value })} placeholder="资料标题" /><input aria-label={`资料 ${index + 1} 链接`} type="url" className={inputClassName} maxLength={2000} value={item.url} onChange={event => editMaterial(item.id, { url: event.target.value })} placeholder="https://…" /><textarea aria-label={`资料 ${index + 1} 摘要`} className={fieldClass} rows={2} maxLength={5000} value={item.notes} onChange={event => editMaterial(item.id, { notes: event.target.value })} placeholder="这份资料有哪些值得记录的观点或证据？" /></div>)}
        </div>
        <label className="block space-y-2"><span className="text-body2 font-medium">分析笔记</span><textarea aria-label="分析笔记" className={fieldClass} rows={7} maxLength={30000} value={draft.notes} onChange={event => edit({ notes: event.target.value })} placeholder="整理发现、比较不同观点，记录需要继续验证的问题…" /></label>
        <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-4"><span className="text-body2 text-muted-foreground">{dirty ? "有尚未保存的修改" : state.owner ? "草稿保存在当前账号的研究工作区" : "草稿保存在当前浏览器的研究工作区"}</span><Button type="button" onClick={save}><Save size={15} strokeWidth={1.8} />保存草稿</Button></div>
        {notice && <p role="status" className="flex items-center gap-2 text-body2 text-success-text"><Check size={15} strokeWidth={1.8} />{state.owner && state.status === "synced" ? "已同步到账号，可以随时继续编辑。" : notice}</p>}{(error || (state.owner && state.message)) && <p role="alert" className="text-body2 text-destructive-text">{error || state.message}</p>}
      </div>
      <aside className="space-y-6"><div className="rounded-xl border p-4"><h3 className="text-body2 font-semibold">继续收集资料</h3><p className="mb-3 mt-1 text-body2 text-muted-foreground">围绕当前主题查找论文，资料页在新标签中打开。</p><Button asChild variant="secondary" size="sm"><a href={researchQuestionHref("papers", draft.title || draft.question)} target="_blank" rel="noreferrer"><BookOpen size={15} strokeWidth={1.8} />查找相关论文</a></Button></div><div><h3 className="mb-2 px-1 text-[11px] font-semibold uppercase tracking-wider text-faint-foreground">已保存的准备稿</h3>{drafts.length === 0 ? <p className="px-1 text-body2 text-muted-foreground">保存后可在这里继续编辑。</p> : <ul className="divide-y rounded-xl border">{drafts.map(saved => <li key={saved.id} className="flex items-start gap-2 px-3 py-2.5"><button type="button" onClick={() => open(saved)} className="min-w-0 flex-1 rounded-sm text-left text-body2 font-medium text-foreground outline-none hover:text-foreground/80 focus-visible:ring-3 focus-visible:ring-ring/50"><span className="line-clamp-2">{saved.title}</span><span className="mt-0.5 block text-label font-normal text-faint-foreground">{new Date(saved.updatedAt).toLocaleString("zh-CN")}</span></button><button type="button" aria-label={`删除草稿：${saved.title}`} onClick={() => { if (dirty) { setError("请先保存当前修改。"); return } if (!deleteReportDraft(saved.id)) setError("删除失败，请重试。"); else if (saved.id === draft.id) { setDraft(newReportDraft(question)); setNotice("草稿已删除。") } }} className="grid size-7 shrink-0 place-items-center rounded-full text-muted-foreground outline-none transition-colors hover:bg-destructive/10 hover:text-destructive-text focus-visible:ring-3 focus-visible:ring-ring/50"><Trash2 size={14} strokeWidth={1.8} /></button></li>)}</ul>}</div></aside>
    </div>
    </div>
  </section>
}
