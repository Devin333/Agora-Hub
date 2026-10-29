"use client"

import { useState, type FormEvent, type ReactNode } from "react"
import { useRouter } from "next/navigation"
import { ArrowLeft, ArrowUpRight, BookOpen, Check, ClipboardCheck, FileText, Folder, GitCompareArrows, Github, Pencil, Plus, Quote, Search, Trash2, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input, inputClassName } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import { historyState, removeResearchMaterial, saveResearchMaterial } from "@/lib/research/history"
import { materialKindLabel, prepareMaterialReport, reportDraftHref } from "@/lib/research/materials"
import type { ResearchMaterial } from "@/lib/research/workspace-items"
import { researchHomeHref } from "@/lib/research/routes"
import { cn } from "@/lib/utils"

const icons = { paper: BookOpen, project: Github, discussion: Quote, note: FileText, pdf: FileText }
const focusRing = "outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
const linkClass = cn("inline-flex items-center gap-1 rounded-sm text-body2 text-link underline-offset-2 hover:underline", focusRing)
const fieldClass = "grid gap-2 text-body2 font-medium"
const checkboxClass = "size-4 shrink-0 cursor-pointer accent-primary"

/** Full-page shell shared by every state of the group page: back link header plus a centered column. */
function GroupShell({ status, children }: { status?: ReactNode; children: ReactNode }) {
  return <div className="min-h-svh bg-background text-foreground">
    <header className="flex min-h-[61px] items-center justify-between gap-4 border-b border-border px-4 py-3 md:px-6">
      <Button asChild variant="ghost" size="lg" className="-ms-2.5 gap-2 text-body1"><a href={researchHomeHref()}><ArrowLeft size={16} strokeWidth={1.8} aria-hidden="true" />我的研究</a></Button>
      {status && <span className="text-label text-muted-foreground">{status}</span>}
    </header>
    <main className="mx-auto w-full max-w-[60rem] px-4 py-8 md:px-6 md:py-10">{children}</main>
  </div>
}

export function ResearchGroupPage({ groupId }: { groupId: string | null }) {
  const state = useOwnedResearchWorkspace()
  if (!state.ready) return <GroupShell><p className="py-16 text-center text-body2 text-muted-foreground" role="status">{state.status === "error" ? "研究资料暂时无法读取，请重新登录或稍后重试。" : "正在读取研究资料…"}</p></GroupShell>
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
  const syncStatus = state.owner ? state.status === "synced" ? "已同步到账号" : state.status === "saving" ? "正在同步…" : "尚未同步" : "资料保存在本机"

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
    router.push(`${researchHomeHref()}?${params}`)
  }
  function remove() {
    if (deleting && owned() && removeResearchMaterial(deleting.id, state.owner)) { setSelected(selected.filter(id => id !== deleting.id)); setDeleting(null); setNotice("资料已移除。") }
  }
  if (!title) return <GroupShell><p className="py-16 text-center text-body2 text-muted-foreground">当前账号下找不到这个分组。</p></GroupShell>

  return <GroupShell status={syncStatus}>
    <div className="mb-6 flex flex-wrap items-start justify-between gap-4">
      <div className="min-w-0"><h1 className="break-words text-heading2 font-semibold">{title}</h1><p className="mt-1 text-body2 text-muted-foreground">{materials.length} 项资料 · {drafts.length} 份报告准备稿</p></div>
      <Button type="button" size="lg" onClick={() => edit()}><Plus aria-hidden="true" />添加资料</Button>
    </div>
    <div className="flex flex-wrap items-center gap-2">
      <label className="flex h-9 min-w-[240px] flex-1 items-center gap-2 rounded-lg border border-input bg-muted px-3 text-muted-foreground transition-colors focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50">
        <Search size={15} strokeWidth={1.8} aria-hidden="true" />
        <input aria-label="搜索分组资料" placeholder="搜索标题、笔记" value={search} onChange={e => setSearch(e.target.value)} className="min-w-0 flex-1 bg-transparent text-sm text-foreground outline-none placeholder:text-muted-foreground" />
      </label>
      <select className={cn(inputClassName, "w-auto cursor-pointer pe-8")} aria-label="资料类型" value={kind} onChange={e => setKind(e.target.value)}><option value="all">全部类型</option>{Object.entries(materialKindLabel).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
    </div>
    <div className="flex min-h-14 flex-wrap items-center gap-2 py-3 text-body2 text-muted-foreground">
      <label className="me-2 inline-flex cursor-pointer items-center gap-2"><input type="checkbox" className={checkboxClass} aria-label="全选当前资料" checked={visible.length > 0 && visible.every(m => selected.includes(m.id))} onChange={e => setSelected(e.target.checked ? [...new Set([...selected, ...visible.map(m => m.id)])] : selected.filter(id => !visible.some(m => m.id === id)))} />已选 {chosen.length} 项</label>
      <Button type="button" variant="outline" size="sm" disabled={chosen.length < 2 || chosen.length > 4} onClick={() => setCompare(true)} title="选择 2 至 4 项资料进行对比"><GitCompareArrows aria-hidden="true" />对比资料</Button>
      <Button type="button" variant="outline" size="sm" disabled={!chosen.length} onClick={useContext}><Folder aria-hidden="true" />带入提问</Button>
      <Button type="button" variant="outline" size="sm" disabled={!chosen.length || chosen.length > 100} onClick={report}><ClipboardCheck aria-hidden="true" />准备报告</Button>
    </div>
    {error && !editor && <p role="alert" className="mb-3 text-body2 text-destructive-text">{error}</p>}
    {state.message && <p role="alert" className="mb-3 text-body2 text-destructive-text">{state.message}</p>}
    {notice && <p role="status" className="mb-3 text-body2 text-success-text">{notice}</p>}
    {visible.length
      ? <ul className="divide-y divide-border overflow-hidden rounded-xl border border-border">{visible.map(m => {
        const Icon = icons[m.kind]
        return <li className="group/row flex items-start gap-3 px-4 py-4 transition-colors hover:bg-accent/30" key={m.id}>
          <input type="checkbox" className={cn(checkboxClass, "mt-2.5")} aria-label={`选择资料：${m.title}`} checked={selected.includes(m.id)} onChange={e => setSelected(e.target.checked ? [...selected, m.id] : selected.filter(id => id !== m.id))} />
          <span className="grid size-9 shrink-0 place-items-center rounded-full bg-muted text-muted-foreground ring-1 ring-foreground/10"><Icon size={16} strokeWidth={1.8} aria-hidden="true" /></span>
          <div className="min-w-0 flex-1">
            <h3 className="break-words text-body1 font-medium">{m.title}</h3>
            <p className="mt-0.5 text-label text-faint-foreground">{materialKindLabel[m.kind]} · {new Date(m.updatedAt).toLocaleDateString("zh-CN")}</p>
            {m.notes && <p className="mt-2 whitespace-pre-wrap break-words text-body2 text-muted-foreground">{m.notes}</p>}
            {(m.url || m.readerHref) && <div className="mt-2 flex flex-wrap gap-3">
              {m.url && <a className={linkClass} href={m.url} target="_blank" rel="noreferrer">查看来源<ArrowUpRight size={13} strokeWidth={1.8} aria-hidden="true" /></a>}
              {m.readerHref && <a className={linkClass} href={m.readerHref}><BookOpen size={13} strokeWidth={1.8} aria-hidden="true" />继续阅读</a>}
            </div>}
          </div>
          <div className="flex shrink-0 items-center gap-0.5">
            <Button type="button" variant="ghost" size="icon-sm" className="rounded-full text-muted-foreground" aria-label={`编辑资料：${m.title}`} title="编辑资料" onClick={() => edit(m)}><Pencil aria-hidden="true" /></Button>
            <Button type="button" variant="ghost" size="icon-sm" className="rounded-full text-muted-foreground hover:bg-destructive/10 hover:text-destructive" aria-label={`移除资料：${m.title}`} title="移除资料" onClick={() => setDeleting(m)}><Trash2 aria-hidden="true" /></Button>
          </div>
        </li>
      })}</ul>
      : <div className="rounded-xl border border-dashed border-border px-6 py-14 text-center text-body2 text-muted-foreground">
        {search || kind !== "all" ? "没有匹配的资料" : "还没有资料"}
        {search && <p className="mt-3"><Button type="button" variant="outline" size="sm" onClick={() => { setSearch(""); setKind("all") }}>查看全部资料</Button></p>}
      </div>}
    <section className="mt-10">
      <h2 className="mb-3 text-heading3 font-semibold">报告准备稿</h2>
      {drafts.length
        ? <ul className="divide-y divide-border overflow-hidden rounded-xl border border-border">{drafts.map(d => <li key={d.id}><a href={reportDraftHref(d)} className={cn("flex items-center justify-between gap-4 px-4 py-3 text-body2 transition-colors hover:bg-accent/30", focusRing)}><span className="min-w-0 truncate font-medium">{d.title}</span><span className="shrink-0 tabular-nums text-label text-faint-foreground">{new Date(d.updatedAt).toLocaleDateString("zh-CN")}</span></a></li>)}</ul>
        : <p className="text-body2 text-muted-foreground">暂无报告准备稿</p>}
    </section>
    <Dialog open={Boolean(editor)} onOpenChange={open => { if (!open) setEditor(null) }}>
      <DialogContent showCloseButton={false} className="max-h-[calc(100svh-4rem)] overflow-y-auto sm:max-w-lg">
        <DialogHeader className="pe-8"><DialogTitle>{editor && materials.some(m => m.id === editor.id) ? "编辑资料" : "添加资料"}</DialogTitle><DialogDescription>{title}</DialogDescription></DialogHeader>
        {editor && <form className="grid gap-4" onSubmit={save}>
          <label className={fieldClass}>类型<select className={cn(inputClassName, "cursor-pointer")} value={editor.kind} onChange={e => setEditor({ ...editor, kind: e.target.value as ResearchMaterial["kind"] })}>{Object.entries(materialKindLabel).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label className={fieldClass}>标题<Input autoFocus required maxLength={500} value={editor.title} onChange={e => setEditor({ ...editor, title: e.target.value })} /></label>
          <label className={fieldClass}>来源链接<Input type="url" required={editor.kind !== "note" && !editor.readerHref} maxLength={2000} value={editor.url} placeholder="https://" onChange={e => setEditor({ ...editor, url: e.target.value })} /></label>
          <label className={fieldClass}>笔记<textarea className={cn(inputClassName, "rk-scroll h-auto min-h-[120px] resize-y py-2")} maxLength={10000} value={editor.notes} onChange={e => setEditor({ ...editor, notes: e.target.value })} /></label>
          <label className={fieldClass}>分组<select className={cn(inputClassName, "cursor-pointer")} value={editor.groupId ?? ""} onChange={e => setEditor({ ...editor, groupId: e.target.value || null })}><option value="">未分组</option>{state.workspace.groups.map(g => <option value={g.id} key={g.id}>{g.name}</option>)}</select></label>
          {error && <p className="text-body2 text-destructive-text" role="alert">{error}</p>}
          <DialogFooter><DialogClose asChild><Button type="button" variant="outline">取消</Button></DialogClose><Button type="submit"><Check aria-hidden="true" />保存</Button></DialogFooter>
        </form>}
        <DialogClose asChild><Button variant="ghost" size="icon-sm" className="absolute right-[18px] top-[18px]" aria-label="关闭资料编辑"><X /></Button></DialogClose>
      </DialogContent>
    </Dialog>
    <Dialog open={Boolean(deleting)} onOpenChange={open => { if (!open) setDeleting(null) }}>
      <DialogContent showCloseButton={false}>
        <DialogHeader><DialogTitle>移除资料</DialogTitle><DialogDescription>移除“{deleting?.title}”？已经保存在报告中的资料副本不会受影响。</DialogDescription></DialogHeader>
        <DialogFooter><DialogClose asChild><Button type="button" variant="outline">取消</Button></DialogClose><Button type="button" variant="destructive" onClick={remove}>移除</Button></DialogFooter>
      </DialogContent>
    </Dialog>
    <Dialog open={compare} onOpenChange={setCompare}>
      <DialogContent showCloseButton={false} className="max-h-[calc(100svh-4rem)] overflow-y-auto sm:max-w-[min(1100px,calc(100vw-4rem))]">
        <DialogHeader className="pe-8"><DialogTitle>资料对比</DialogTitle><DialogDescription>已保存的来源与笔记</DialogDescription></DialogHeader>
        <div className="overflow-hidden rounded-xl border border-border">
          <Table className="table-fixed">
            <TableHeader><TableRow className="bg-muted/50 hover:bg-muted/50"><TableHead className="w-28 px-3">对比项</TableHead>{chosen.map(m => <TableHead key={m.id} className="whitespace-normal break-words px-3">{m.title}</TableHead>)}</TableRow></TableHeader>
            <TableBody>
              <CompareRow label="类型">{chosen.map(m => <TableCell key={m.id} className="px-3 py-3 align-top">{materialKindLabel[m.kind]}</TableCell>)}</CompareRow>
              <CompareRow label="来源">{chosen.map(m => <TableCell key={m.id} className="break-words px-3 py-3 align-top">{m.url ? <a className={linkClass} href={m.url} target="_blank" rel="noreferrer">{m.url}</a> : "未提供"}</TableCell>)}</CompareRow>
              <CompareRow label="资料标识">{chosen.map(m => <TableCell key={m.id} className="break-words px-3 py-3 align-top">{m.referenceId || "未记录"}</TableCell>)}</CompareRow>
              <CompareRow label="研究笔记">{chosen.map(m => <TableCell key={m.id} className="whitespace-pre-wrap break-words px-3 py-3 align-top">{m.notes || "尚未记录"}</TableCell>)}</CompareRow>
            </TableBody>
          </Table>
        </div>
        <DialogFooter><Button type="button" onClick={report}><ClipboardCheck aria-hidden="true" />带入报告准备稿</Button></DialogFooter>
        <DialogClose asChild><Button variant="ghost" size="icon-sm" className="absolute right-[18px] top-[18px]" aria-label="关闭资料对比"><X /></Button></DialogClose>
      </DialogContent>
    </Dialog>
  </GroupShell>
}

function CompareRow({ label, children }: { label: string; children: ReactNode }) {
  return <TableRow className="hover:bg-transparent"><TableHead scope="row" className="h-auto px-3 py-3 align-top text-muted-foreground">{label}</TableHead>{children}</TableRow>
}
