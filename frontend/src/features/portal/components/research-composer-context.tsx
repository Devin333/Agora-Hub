"use client"

import { useState } from "react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Input, inputClassName } from "@/components/ui/input"
import { Folder, Link2, Paperclip, Plus, Search, Upload, X, type LucideIcon } from "lucide-react"
import { materialKindLabel } from "@/lib/research/materials"
import { historyState, saveResearchMaterial } from "@/lib/research/history"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import { cn } from "@/lib/utils"

const focusRing = "outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
/** The rakazo composer attach button: a small round well on the bar's leading edge. */
const attachClass = cn("grid size-8 shrink-0 place-items-center self-center rounded-full border border-border bg-muted text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:pointer-events-none disabled:opacity-50 data-[state=open]:bg-accent data-[state=open]:text-foreground", focusRing)
const selectClass = cn(inputClassName, "h-8 w-auto cursor-pointer pe-7")

export function ResearchComposerContext({ groupId, setGroupId, materialIds, setMaterialIds, disabled }: {
  groupId: string | null; setGroupId: (id: string | null) => void
  materialIds: string[]; setMaterialIds: (ids: string[]) => void; disabled: boolean
}) {
  const state = useOwnedResearchWorkspace()
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState("")
  const [scope, setScope] = useState("all")
  const [linkOpen, setLinkOpen] = useState(false)
  const [link, setLink] = useState("")
  const [linkError, setLinkError] = useState("")
  const materials = state.workspace.materials ?? []
  const selected = materials.filter(m => materialIds.includes(m.id))
  const visible = materials.filter(m => (scope === "all" || m.groupId === scope) && `${m.title} ${m.notes}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()))
  function requestUpload() { window.dispatchEvent(new Event("agora-open-pdf-import")) }
  function addLink() {
    const value = supportedResearchLink(link)
    if (!value) { setLinkError("请添加 arXiv 论文、DOI 或 GitHub 项目链接。"); return }
    if (selected.length >= 50) { setLinkError("本次最多引用 50 份资料，请先移除一些。"); return }
    const existing = materials.find(item => item.url === value)
    const now = Date.now(), id = existing?.id ?? crypto.randomUUID()
    const url = new URL(value)
    if (!existing && !saveResearchMaterial({ id, groupId: null, kind: url.hostname === "github.com" ? "project" : "paper", title: url.hostname === "github.com" ? url.pathname.slice(1) : `${url.hostname} ${url.pathname.split("/").pop()}`, url: value, notes: "", createdAt: now, updatedAt: now }, state.owner)) {
      setLinkError(historyState().message || "暂时无法保存链接，请重试。"); return
    }
    setMaterialIds([...new Set([...materialIds, id])]); setLink(""); setLinkError(""); setLinkOpen(false)
  }
  return <div className="contents">
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger type="button" disabled={disabled} className={attachClass} aria-label="添加到本次研究" title="添加到本次研究"><Plus size={16} strokeWidth={2} aria-hidden="true" /></DropdownMenuTrigger>
      <DropdownMenuContent align="start" sideOffset={8} className="w-72 p-1.5">
        <DropdownMenuLabel>添加到本次研究</DropdownMenuLabel>
        <DropdownMenuItem onSelect={requestUpload} className="gap-2.5 px-2 py-2"><Upload size={15} strokeWidth={1.8} aria-hidden="true" />上传 PDF</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => { setLinkError(""); setLinkOpen(true) }} className="gap-2.5 px-2 py-2"><Link2 size={15} strokeWidth={1.8} aria-hidden="true" />添加论文或项目链接</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => setOpen(true)} className="gap-2.5 px-2 py-2"><Paperclip size={15} strokeWidth={1.8} aria-hidden="true" />选择研究资料{selected.length ? ` (${selected.length})` : ""}</DropdownMenuItem>
        <DropdownMenuSeparator />
        <label className="flex items-center gap-2.5 px-2 py-1.5 text-sm" onPointerDown={event => event.stopPropagation()}>
          <Folder size={15} strokeWidth={1.8} className="text-muted-foreground" aria-hidden="true" /><span className="flex-1">保存到</span>
          <select aria-label="新研究的保存分组" className={cn(selectClass, "max-w-[150px]")} value={groupId ?? ""} onChange={event => setGroupId(event.target.value || null)}><option value="">未分组</option>{state.workspace.groups.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}</select>
        </label>
      </DropdownMenuContent>
    </DropdownMenu>
    <Dialog open={open && state.ready} onOpenChange={setOpen}>
      <DialogContent showCloseButton={false} className="max-h-[calc(100svh-4rem)] grid-rows-[auto_auto_minmax(0,1fr)_auto] sm:max-w-xl">
        <DialogHeader className="pe-8"><DialogTitle>选择研究资料</DialogTitle><DialogDescription>选择本次提问要参考的资料</DialogDescription></DialogHeader>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex h-9 min-w-[220px] flex-1 items-center gap-2 rounded-lg border border-input bg-muted px-3 text-muted-foreground transition-colors focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50">
            <Search size={15} strokeWidth={1.8} aria-hidden="true" />
            <input aria-label="搜索可用研究资料" value={search} placeholder="搜索标题、笔记" onChange={e => setSearch(e.target.value)} className="min-w-0 flex-1 bg-transparent text-sm text-foreground outline-none placeholder:text-muted-foreground" />
          </label>
          <select aria-label="研究资料分组" className={cn(selectClass, "h-9")} value={scope} onChange={e => setScope(e.target.value)}><option value="all">全部分组</option>{state.workspace.groups.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}</select>
        </div>
        <ul className="rk-scroll -mx-2 max-h-[300px] min-h-0 overflow-y-auto">
          {visible.map(m => <li key={m.id}>
            <label className="flex cursor-pointer items-start gap-3 rounded-xl px-2 py-2.5 transition-colors hover:bg-accent/60 has-[:disabled]:cursor-not-allowed has-[:disabled]:opacity-50">
              <input type="checkbox" className="mt-1 size-4 shrink-0 accent-primary" checked={materialIds.includes(m.id)} disabled={!materialIds.includes(m.id) && selected.length >= 50} onChange={e => setMaterialIds(e.target.checked ? [...new Set([...materialIds, m.id])] : materialIds.filter(id => id !== m.id))} />
              <span className="min-w-0"><span className="block break-words text-body2 font-medium">{m.title}</span><span className="mt-0.5 block text-label text-muted-foreground">{materialKindLabel[m.kind]} · {state.workspace.groups.find(g => g.id === m.groupId)?.name ?? "未分组"}</span></span>
            </label>
          </li>)}
          {!visible.length && <li className="px-2 py-10 text-center text-body2 text-muted-foreground">{search ? "没有匹配的资料" : "暂无已保存资料"}</li>}
        </ul>
        <DialogFooter><DialogClose asChild><Button type="button">完成</Button></DialogClose></DialogFooter>
        <DialogClose asChild><Button variant="ghost" size="icon-sm" className="absolute right-[18px] top-[18px]" aria-label="关闭研究资料选择"><X /></Button></DialogClose>
      </DialogContent>
    </Dialog>
    <Dialog open={linkOpen} onOpenChange={setLinkOpen}>
      <DialogContent showCloseButton={false}>
        <DialogHeader className="pe-8"><DialogTitle>添加论文或项目链接</DialogTitle><DialogDescription>粘贴 arXiv、DOI 或 GitHub 链接</DialogDescription></DialogHeader>
        <form className="grid gap-4" onSubmit={event => { event.preventDefault(); event.stopPropagation(); addLink() }}>
          <label className="grid gap-2 text-body2 font-medium">链接<Input required type="url" value={link} placeholder="https://arxiv.org/..." onChange={event => { setLink(event.target.value); setLinkError("") }} aria-invalid={linkError ? true : undefined} aria-describedby={linkError ? "research-link-error" : undefined} /></label>
          {linkError && <p id="research-link-error" role="alert" className="text-body2 text-destructive-text">{linkError}</p>}
          <DialogFooter><DialogClose asChild><Button type="button" variant="outline">取消</Button></DialogClose><Button type="submit">添加</Button></DialogFooter>
        </form>
        <DialogClose asChild><Button variant="ghost" size="icon-sm" className="absolute right-[18px] top-[18px]" aria-label="关闭链接添加"><X /></Button></DialogClose>
      </DialogContent>
    </Dialog>
  </div>
}

export function ResearchContextSummary({ groupId, setGroupId, materialIds, setMaterialIds, disabled }: {
  groupId: string | null; setGroupId: (id: string | null) => void
  materialIds: string[]; setMaterialIds: (ids: string[]) => void; disabled: boolean
}) {
  const state = useOwnedResearchWorkspace()
  const group = state.workspace.groups.find(item => item.id === groupId)
  const selected = (state.workspace.materials ?? []).filter(item => materialIds.includes(item.id))
  if (!group && !selected.length) return null
  return <ul className="mt-3 flex list-none flex-wrap gap-2 p-0" aria-label="本次研究选中的资料">
    {group && <ContextChip icon={Folder} label={`保存到：${group.name}`} removeLabel="取消保存分组" disabled={disabled} onRemove={() => setGroupId(null)} />}
    {selected.map(m => <ContextChip key={m.id} icon={Paperclip} label={m.title} removeLabel={`取消携带资料：${m.title}`} disabled={disabled} onRemove={() => setMaterialIds(materialIds.filter(id => id !== m.id))} />)}
  </ul>
}

/** A removable attachment chip under the composer, like rakazo's attachment pills. */
function ContextChip({ icon: Icon, label, removeLabel, disabled, onRemove }: { icon: LucideIcon; label: string; removeLabel: string; disabled: boolean; onRemove: () => void }) {
  return <li className="flex h-8 max-w-full items-center gap-1.5 rounded-full border border-border bg-muted pe-1 ps-3 text-body2 text-foreground/75">
    <Icon size={14} strokeWidth={1.8} className="shrink-0 text-muted-foreground" aria-hidden="true" />
    <span className="max-w-[260px] truncate" title={label}>{label}</span>
    <button type="button" disabled={disabled} aria-label={removeLabel} onClick={onRemove} className={cn("grid size-6 shrink-0 place-items-center rounded-full text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50", focusRing)}><X size={13} strokeWidth={2} aria-hidden="true" /></button>
  </li>
}

export function supportedResearchLink(value: string): string | null {
  try {
    const url = new URL(value.trim())
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password || url.port || value.length > 2000) return null
    if (url.hostname === "github.com" && /^\/[\w.-]+\/[\w.-]+\/?$/.test(url.pathname)) return `https://github.com${url.pathname.replace(/\/$/, "")}`
    if ((url.hostname === "arxiv.org" || url.hostname === "www.arxiv.org") && /^\/(?:abs|pdf)\/(?:\d{4}\.\d{4,5}(?:v\d+)?|[a-z.-]+\/\d{7}(?:v\d+)?)(?:\.pdf)?\/?$/i.test(url.pathname)) return `https://arxiv.org${url.pathname}`
    if (url.hostname === "doi.org" && /^\/10\.\d{4,9}\/.+/i.test(url.pathname)) return `https://doi.org${url.pathname}`
  } catch { /* Invalid links remain editable. */ }
  return null
}
