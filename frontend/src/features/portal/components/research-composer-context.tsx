"use client"

import { useState } from "react"
import * as Dialog from "@radix-ui/react-dialog"
import { Folder, Paperclip, Search, X } from "lucide-react"
import { materialKindLabel } from "@/lib/research/materials"
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
  const materials = state.workspace.materials ?? []
  const selected = materials.filter(m => materialIds.includes(m.id))
  const visible = materials.filter(m => (scope === "all" || m.groupId === scope) && `${m.title} ${m.notes}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()))
  return <div className={styles.composerContext}>
    <label className={styles.destination}><Folder size={16} /><span>保存到</span><select aria-label="新研究的保存分组" disabled={disabled} value={groupId ?? ""} onChange={e => setGroupId(e.target.value || null)}><option value="">未分组</option>{state.workspace.groups.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}</select></label>
    <Dialog.Root open={open && state.ready} onOpenChange={setOpen}>
      <Dialog.Trigger type="button" disabled={disabled} className={styles.button}><Paperclip size={16} />研究资料{selected.length ? ` (${selected.length})` : ""}</Dialog.Trigger>
      <Dialog.Portal><Dialog.Overlay className={styles.overlay} /><Dialog.Content className={styles.dialog}>
        <Dialog.Title>选择研究资料</Dialog.Title><Dialog.Description className={styles.description}>已选 {selected.length} / 50 项</Dialog.Description>
        <div className={styles.toolbar}><label className={styles.search}><Search size={16} /><input aria-label="搜索可用研究资料" value={search} placeholder="搜索标题、笔记" onChange={e => setSearch(e.target.value)} /></label><select aria-label="研究资料分组" className={styles.select} value={scope} onChange={e => setScope(e.target.value)}><option value="all">全部分组</option>{state.workspace.groups.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}</select></div>
        <ul className={styles.materialChoices}>{visible.map(m => <li key={m.id}><label><input type="checkbox" checked={materialIds.includes(m.id)} disabled={!materialIds.includes(m.id) && selected.length >= 50} onChange={e => setMaterialIds(e.target.checked ? [...new Set([...materialIds, m.id])] : materialIds.filter(id => id !== m.id))} /><span><strong>{m.title}</strong><small>{materialKindLabel[m.kind]} · {state.workspace.groups.find(g => g.id === m.groupId)?.name ?? "未分组"}</small></span></label></li>)}</ul>
        {!visible.length && <p className={styles.empty}>{search ? "没有匹配的资料" : "暂无已保存资料"}</p>}
        <div className={styles.dialogActions}><Dialog.Close className={styles.primary}>完成</Dialog.Close></div>
        <Dialog.Close className={`${styles.iconButton} ${styles.close}`} aria-label="关闭研究资料选择"><X size={18} /></Dialog.Close>
      </Dialog.Content></Dialog.Portal>
    </Dialog.Root>
    {selected.length > 0 && <ul className={styles.attachedMaterials} aria-label="本次研究选中的资料">{selected.map(m => <li key={m.id}><Paperclip size={14} /><span title={m.title}>{m.title}</span><button type="button" disabled={disabled} aria-label={`取消携带资料：${m.title}`} onClick={() => setMaterialIds(materialIds.filter(id => id !== m.id))}><X size={14} /></button></li>)}</ul>}
  </div>
}
