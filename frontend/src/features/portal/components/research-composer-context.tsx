"use client"

import { useState } from "react"
import * as Dialog from "@radix-ui/react-dialog"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Folder, Paperclip, Plus, Search, Upload, X } from "lucide-react"
import { materialKindLabel } from "@/lib/research/materials"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import styles from "./research-workspace.module.css"

export function ResearchComposerContext({ groupId, setGroupId, materialIds, setMaterialIds, disabled, onAddLink }: {
  groupId: string | null; setGroupId: (id: string | null) => void
  materialIds: string[]; setMaterialIds: (ids: string[]) => void; disabled: boolean; onAddLink?: (link: string) => void
}) {
  const state = useOwnedResearchWorkspace()
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState("")
  const [scope, setScope] = useState("all")
  const [linkOpen, setLinkOpen] = useState(false)
  const [link, setLink] = useState("")
  const materials = state.workspace.materials ?? []
  const selected = materials.filter(m => materialIds.includes(m.id))
  const visible = materials.filter(m => (scope === "all" || m.groupId === scope) && `${m.title} ${m.notes}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()))
  function requestUpload() { window.dispatchEvent(new Event("agora-open-pdf-import")) }
  return <div className={styles.composerContext}>
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger type="button" disabled={disabled} className={styles.addTrigger} aria-label="添加到本次研究" title="添加到本次研究"><Plus size={18} /></DropdownMenuTrigger>
      <DropdownMenuContent align="start" sideOffset={8} className={styles.addMenu}>
        <p className={styles.addMenuTitle}>添加到本次研究</p>
        <DropdownMenuItem onSelect={requestUpload} className={styles.addMenuItem}><Upload size={16} />上传 PDF</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => setLinkOpen(true)} className={styles.addMenuItem}><Search size={16} />添加论文或项目链接</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => setOpen(true)} className={styles.addMenuItem}><Paperclip size={16} />选择研究资料{selected.length ? ` (${selected.length})` : ""}</DropdownMenuItem>
        <div className={styles.addMenuDivider} />
        <label className={styles.addMenuSelect} onPointerDown={event => event.stopPropagation()}><Folder size={16} /><span>保存到</span><select aria-label="新研究的保存分组" value={groupId ?? ""} onChange={event => setGroupId(event.target.value || null)}><option value="">未分组</option>{state.workspace.groups.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}</select></label>
      </DropdownMenuContent>
    </DropdownMenu>
    {selected.length > 0 && <ul className={styles.attachedMaterials} aria-label="本次研究选中的资料">{selected.map(m => <li key={m.id}><Paperclip size={14} /><span title={m.title}>{m.title}</span><button type="button" disabled={disabled} aria-label={`取消携带资料：${m.title}`} onClick={() => setMaterialIds(materialIds.filter(id => id !== m.id))}><X size={14} /></button></li>)}</ul>}
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
        <form onSubmit={event => { event.preventDefault(); const value = link.trim(); if (!/^https?:\/\/\S+$/i.test(value)) return; onAddLink?.(value); setLink(""); setLinkOpen(false) }}><label className={styles.field}>链接<input autoFocus required type="url" value={link} placeholder="https://arxiv.org/..." onChange={event => setLink(event.target.value)} /></label><div className={styles.dialogActions}><Dialog.Close className={styles.button} type="button">取消</Dialog.Close><button className={styles.primary} type="submit">添加</button></div></form><Dialog.Close className={`${styles.iconButton} ${styles.close}`} aria-label="关闭链接添加"><X size={18} /></Dialog.Close>
      </Dialog.Content></Dialog.Portal>
    </Dialog.Root>
  </div>
}
