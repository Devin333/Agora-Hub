"use client"

import { useState } from "react"
import * as Dialog from "@radix-ui/react-dialog"
import { BookmarkPlus, BookMarked, Trash2, X } from "lucide-react"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import { removeResearchPrompt, saveResearchPrompt } from "@/lib/research/history"
import type { ResearchPrompt, ResearchConstraints } from "@/lib/research/workspace-items"
import type { ResearchMode } from "@/lib/research/entry"
import styles from "./research-workspace.module.css"

export function ResearchPromptMenu({ question, mode, constraints, onSelect }: { question: string; mode: ResearchMode; constraints: ResearchConstraints; onSelect: (prompt: ResearchPrompt) => void }) {
  const state = useOwnedResearchWorkspace()
  const [open, setOpen] = useState(false)
  const [name, setName] = useState("")
  const [error, setError] = useState("")
  const [editOwner, setEditOwner] = useState(state.owner)
  return <>
    <DropdownMenu><DropdownMenuTrigger className={styles.iconButton} disabled={!state.ready} aria-label="常用提问" title="常用提问"><BookMarked size={18} /></DropdownMenuTrigger><DropdownMenuContent className="w-80 border-[#e0d4ec] bg-white p-2 text-[#5c4774]" align="end"><DropdownMenuItem disabled={!question.trim()} onSelect={() => { setName(question.trim().slice(0, 30)); setError(""); setEditOwner(state.owner); setOpen(true) }}><BookmarkPlus size={16} className="mr-2" />保存当前提问</DropdownMenuItem>{(state.workspace.prompts ?? []).map(prompt => <div key={prompt.id} className="flex items-center"><DropdownMenuItem className="min-w-0 flex-1" onSelect={() => onSelect(prompt)}><span className="truncate">{prompt.name}</span></DropdownMenuItem><button className={styles.iconButton} aria-label={`删除常用提问：${prompt.name}`} title="删除常用提问" onClick={() => removeResearchPrompt(prompt.id, state.owner)}><Trash2 size={15} /></button></div>)}{!state.workspace.prompts?.length && <p className="px-2 py-3 text-sm text-[#857191]">还没有常用提问</p>}</DropdownMenuContent></DropdownMenu>
    <Dialog.Root open={open && editOwner === state.owner} onOpenChange={setOpen}><Dialog.Portal><Dialog.Overlay className={styles.overlay} /><Dialog.Content className={styles.dialog}><Dialog.Title>保存常用提问</Dialog.Title><Dialog.Description className={styles.description}>{question}</Dialog.Description><form onSubmit={event => { event.preventDefault(); if (saveResearchPrompt({ id: crypto.randomUUID(), name: name.trim(), question, mode, constraints, updatedAt: Date.now() }, editOwner)) setOpen(false); else setError("保存失败，请检查名称或账号状态。") }}><label className={styles.field}>名称<input autoFocus required maxLength={80} value={name} onChange={event => setName(event.target.value)} /></label>{error && <p className={styles.error} role="alert">{error}</p>}<div className={styles.dialogActions}><Dialog.Close className={styles.button} type="button">取消</Dialog.Close><button className={styles.primary} type="submit">保存</button></div></form><Dialog.Close className={`${styles.close} ${styles.iconButton}`} aria-label="关闭常用提问编辑"><X size={18} /></Dialog.Close></Dialog.Content></Dialog.Portal></Dialog.Root>
  </>
}
