"use client"

import { useState } from "react"
import * as Dialog from "@radix-ui/react-dialog"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Folder, Link2, Paperclip, Plus, Search, Upload, X } from "lucide-react"
import { materialKindLabel } from "@/lib/research/materials"
import { historyState, saveResearchMaterial } from "@/lib/research/history"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import styles from "./research-workspace.module.css"

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
  return <div className={styles.composerContext}>
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger type="button" disabled={disabled} className={styles.addTrigger} aria-label="添加到本次研究" title="添加到本次研究"><Plus size={18} /></DropdownMenuTrigger>
      <DropdownMenuContent align="start" sideOffset={8} className={styles.addMenu}>
        <p className={styles.addMenuTitle}>添加到本次研究</p>
        <DropdownMenuItem onSelect={requestUpload} className={styles.addMenuItem}><Upload size={16} />上传 PDF</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => { setLinkError(""); setLinkOpen(true) }} className={styles.addMenuItem}><Link2 size={16} />添加论文或项目链接</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => setOpen(true)} className={styles.addMenuItem}><Paperclip size={16} />选择研究资料{selected.length ? ` (${selected.length})` : ""}</DropdownMenuItem>
        <div className={styles.addMenuDivider} />
        <label className={styles.addMenuSelect} onPointerDown={event => event.stopPropagation()}><Folder size={16} /><span>保存到</span><select aria-label="新研究的保存分组" value={groupId ?? ""} onChange={event => setGroupId(event.target.value || null)}><option value="">未分组</option>{state.workspace.groups.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}</select></label>
      </DropdownMenuContent>
    </DropdownMenu>
    <Dialog.Root open={open && state.ready} onOpenChange={setOpen}>
      <Dialog.Portal><Dialog.Overlay className={styles.overlay} /><Dialog.Content className={styles.dialog}>
        <Dialog.Title>选择研究资料</Dialog.Title><Dialog.Description className={styles.description}>选择本次提问要参考的资料</Dialog.Description>
        <div className={styles.toolbar}><label className={styles.search}><Search size={16} /><input aria-label="搜索可用研究资料" value={search} placeholder="搜索标题、笔记" onChange={e => setSearch(e.target.value)} /></label><select aria-label="研究资料分组" className={styles.select} value={scope} onChange={e => setScope(e.target.value)}><option value="all">全部分组</option>{state.workspace.groups.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}</select></div>
        <ul className={styles.materialChoices}>{visible.map(m => <li key={m.id}><label><input type="checkbox" checked={materialIds.includes(m.id)} disabled={!materialIds.includes(m.id) && selected.length >= 50} onChange={e => setMaterialIds(e.target.checked ? [...new Set([...materialIds, m.id])] : materialIds.filter(id => id !== m.id))} /><span><strong>{m.title}</strong><small>{materialKindLabel[m.kind]} · {state.workspace.groups.find(g => g.id === m.groupId)?.name ?? "未分组"}</small></span></label></li>)}</ul>
        {!visible.length && <p className={styles.empty}>{search ? "没有匹配的资料" : "暂无已保存资料"}</p>}
        <div className={styles.dialogActions}><Dialog.Close className={styles.primary}>完成</Dialog.Close></div>
        <Dialog.Close className={`${styles.iconButton} ${styles.close}`} aria-label="关闭研究资料选择"><X size={18} /></Dialog.Close>
      </Dialog.Content></Dialog.Portal>
    </Dialog.Root>
    <Dialog.Root open={linkOpen} onOpenChange={setLinkOpen}>
      <Dialog.Portal><Dialog.Overlay className={styles.overlay} /><Dialog.Content className={styles.dialog}>
        <Dialog.Title>添加论文或项目链接</Dialog.Title><Dialog.Description className={styles.description}>粘贴 arXiv、DOI 或 GitHub 链接</Dialog.Description>
        <form onSubmit={event => { event.preventDefault(); event.stopPropagation(); addLink() }}><label className={styles.field}>链接<input required type="url" value={link} placeholder="https://arxiv.org/..." onChange={event => { setLink(event.target.value); setLinkError("") }} aria-describedby={linkError ? "research-link-error" : undefined} /></label>{linkError && <p id="research-link-error" role="alert" className={styles.error}>{linkError}</p>}<div className={styles.dialogActions}><Dialog.Close className={styles.button} type="button">取消</Dialog.Close><button className={styles.primary} type="submit">添加</button></div></form><Dialog.Close className={`${styles.iconButton} ${styles.close}`} aria-label="关闭链接添加"><X size={18} /></Dialog.Close>
      </Dialog.Content></Dialog.Portal>
    </Dialog.Root>
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
  return <ul className={styles.attachedMaterials} aria-label="本次研究选中的资料">
    {group && <li><Folder size={15} /><span>保存到：{group.name}</span><button type="button" disabled={disabled} aria-label="取消保存分组" onClick={() => setGroupId(null)}><X size={14} /></button></li>}
    {selected.map(m => <li key={m.id}><Paperclip size={15} /><span title={m.title}>{m.title}</span><button type="button" disabled={disabled} aria-label={`取消携带资料：${m.title}`} onClick={() => setMaterialIds(materialIds.filter(id => id !== m.id))}><X size={14} /></button></li>)}
  </ul>
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
