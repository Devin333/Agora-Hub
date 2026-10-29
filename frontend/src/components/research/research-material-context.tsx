"use client"

import { useState } from "react"
import { usePathname, useRouter, useSearchParams } from "next/navigation"
import { ClipboardCheck, Paperclip, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import { materialKindLabel, prepareMaterialReport, reportDraftHref } from "@/lib/research/materials"

export function ResearchMaterialContext() {
  const state = useOwnedResearchWorkspace()
  const params = useSearchParams()
  const router = useRouter()
  const pathname = usePathname()
  const [error, setError] = useState("")
  const ids = [...new Set(params.getAll("material"))].slice(0, 50)
  const materials = (state.workspace.materials ?? []).filter(m => ids.includes(m.id))
  if (!ids.length || !state.ready) return null

  function remove(id: string) {
    const next = new URLSearchParams(params.toString())
    next.delete("material")
    ids.filter(value => value !== id).forEach(value => next.append("material", value))
    router.replace(`${pathname}?${next}`, { scroll: false })
  }
  function report() {
    const requestedGroup = params.get("researchGroup")
    const groupId = state.workspace.groups.some(g => g.id === requestedGroup) ? requestedGroup : null
    const draft = prepareMaterialReport(params.get("question") || "研究资料整理", groupId, materials.map(m => m.id), state.owner)
    if (!draft) { setError("资料准备失败，请检查当前账号和同步状态。"); return }
    router.push(reportDraftHref(draft))
  }

  return <section aria-label="本次研究资料" className="my-4 rounded-xl border text-body2 text-muted-foreground">
    <details className="group">
      <summary className="flex cursor-pointer list-none items-center gap-2 rounded-xl px-4 py-3 font-medium text-foreground outline-none hover:bg-accent/40 focus-visible:ring-3 focus-visible:ring-ring/50 [&::-webkit-details-marker]:hidden">
        <Paperclip size={15} strokeWidth={1.8} className="text-muted-foreground" />本次资料 · {materials.length} 项
      </summary>
      <ul className="divide-y border-t">{materials.map(m => <li key={m.id} className="flex items-start gap-3 px-4 py-3">
        <div className="min-w-0 flex-1">
          <a href={m.readerHref || m.url || undefined} target={!m.readerHref && m.url ? "_blank" : undefined} rel="noreferrer" className="break-words font-medium text-foreground underline-offset-4 hover:underline">{m.title}</a>
          <span className="ml-2 text-label text-faint-foreground">{materialKindLabel[m.kind]}</span>
          {m.notes && <p className="mt-1 whitespace-pre-wrap break-words leading-6">{m.notes}</p>}
        </div>
        <Button type="button" variant="ghost" size="icon-sm" aria-label={`取消携带资料：${m.title}`} onClick={() => remove(m.id)}><X size={15} strokeWidth={1.8} /></Button>
      </li>)}</ul>
      {materials.length > 0 && <div className="border-t px-4 py-3"><Button type="button" variant="outline" size="sm" onClick={report}><ClipboardCheck size={15} strokeWidth={1.8} />用这些资料准备报告</Button></div>}
    </details>
    {materials.length < ids.length && <p role="status" className="border-t px-4 py-3">{ids.length - materials.length} 项资料已移除或不属于当前账号。</p>}
    {error && <p role="alert" className="border-t px-4 py-3 text-destructive-text">{error}</p>}
  </section>
}
