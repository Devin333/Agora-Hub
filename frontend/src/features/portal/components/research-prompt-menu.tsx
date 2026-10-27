"use client"

import { useState } from "react"
import { BookmarkPlus, BookMarked, Trash2, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import { removeResearchPrompt, saveResearchPrompt } from "@/lib/research/history"
import type { ResearchPrompt, ResearchConstraints } from "@/lib/research/workspace-items"
import type { ResearchMode } from "@/lib/research/entry"
import { cn } from "@/lib/utils"

const focusRing = "outline-none focus-visible:ring-3 focus-visible:ring-ring/50"

export function ResearchPromptMenu({ question, mode, constraints, onSelect }: { question: string; mode: ResearchMode; constraints: ResearchConstraints; onSelect: (prompt: ResearchPrompt) => void }) {
  const state = useOwnedResearchWorkspace()
  const [open, setOpen] = useState(false)
  const [name, setName] = useState("")
  const [error, setError] = useState("")
  const [editOwner, setEditOwner] = useState(state.owner)
  const prompts = state.workspace.prompts ?? []
  function save() {
    if (saveResearchPrompt({ id: crypto.randomUUID(), name: name.trim(), question, mode, constraints, updatedAt: Date.now() }, editOwner)) setOpen(false)
    else setError("保存失败，请检查名称或账号状态。")
  }
  return <>
    <DropdownMenu>
      <DropdownMenuTrigger disabled={!state.ready} aria-label="常用提问" title="常用提问" className={cn("grid size-8 shrink-0 place-items-center rounded-full text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:pointer-events-none disabled:opacity-50 data-[state=open]:bg-accent data-[state=open]:text-foreground", focusRing)}>
        <BookMarked size={16} strokeWidth={1.8} aria-hidden="true" />
      </DropdownMenuTrigger>
      <DropdownMenuContent className="w-80 p-1.5" align="end" sideOffset={8}>
        <DropdownMenuItem disabled={!question.trim()} className="gap-2.5 px-2 py-2" onSelect={() => { setName(question.trim().slice(0, 30)); setError(""); setEditOwner(state.owner); setOpen(true) }}><BookmarkPlus size={15} strokeWidth={1.8} aria-hidden="true" />保存当前提问</DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuLabel>常用提问</DropdownMenuLabel>
        {prompts.map(prompt => <div key={prompt.id} className="group/prompt flex items-center gap-1">
          <DropdownMenuItem className="min-w-0 flex-1 px-2 py-2" onSelect={() => onSelect(prompt)}><span className="truncate">{prompt.name}</span></DropdownMenuItem>
          <button type="button" className={cn("grid size-7 shrink-0 place-items-center rounded-full text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive", focusRing)} aria-label={`删除常用提问：${prompt.name}`} title="删除常用提问" onClick={() => removeResearchPrompt(prompt.id, state.owner)}><Trash2 size={14} strokeWidth={1.8} aria-hidden="true" /></button>
        </div>)}
        {!prompts.length && <p className="px-2 py-3 text-body2 text-muted-foreground">还没有常用提问</p>}
      </DropdownMenuContent>
    </DropdownMenu>
    <Dialog open={open && editOwner === state.owner} onOpenChange={setOpen}>
      <DialogContent showCloseButton={false}>
        <DialogHeader className="pe-8"><DialogTitle>保存常用提问</DialogTitle><DialogDescription className="line-clamp-3 break-words">{question}</DialogDescription></DialogHeader>
        <form className="grid gap-4" onSubmit={event => { event.preventDefault(); save() }}>
          <label className="grid gap-2 text-body2 font-medium">名称<Input autoFocus required maxLength={80} value={name} onChange={event => setName(event.target.value)} aria-invalid={error ? true : undefined} /></label>
          {error && <p className="text-body2 text-destructive-text" role="alert">{error}</p>}
          <DialogFooter><DialogClose asChild><Button type="button" variant="outline">取消</Button></DialogClose><Button type="submit">保存</Button></DialogFooter>
        </form>
        <DialogClose asChild><Button variant="ghost" size="icon-sm" className="absolute right-[18px] top-[18px]" aria-label="关闭常用提问编辑"><X /></Button></DialogClose>
      </DialogContent>
    </Dialog>
  </>
}
