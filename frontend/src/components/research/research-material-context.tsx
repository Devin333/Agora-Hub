"use client"

import { useState } from "react"
import { usePathname, useRouter, useSearchParams } from "next/navigation"
import { ClipboardCheck, Paperclip, X } from "lucide-react"
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

  return <section aria-label="本次研究资料" className="my-4 border-y border-[#e7dff1] py-3 text-sm text-[#695d7d]">
    <details><summary className="flex cursor-pointer items-center gap-2 rounded py-1 font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-[#8b5cf6]"><Paperclip size={16} />本次资料 · {materials.length} 项</summary>
      <ul className="mt-2 divide-y divide-[#e7dff1]">{materials.map(m => <li key={m.id} className="flex items-start gap-3 py-3"><div className="min-w-0 flex-1"><a href={m.readerHref || m.url || undefined} target={!m.readerHref && m.url ? "_blank" : undefined} rel="noreferrer" className="break-words font-medium text-[#6d28d9] hover:underline">{m.title}</a><span className="ml-2 text-xs">{materialKindLabel[m.kind]}</span>{m.notes && <p className="mt-1 whitespace-pre-wrap break-words leading-6">{m.notes}</p>}</div><button type="button" aria-label={`取消携带资料：${m.title}`} className="grid size-8 shrink-0 place-items-center rounded hover:bg-[#f0e9ff] focus-visible:ring-2 focus-visible:ring-[#8b5cf6]" onClick={() => remove(m.id)}><X size={15} /></button></li>)}</ul>
      {materials.length > 0 && <button type="button" className="mt-2 inline-flex items-center gap-2 rounded-lg border border-[#ded2ec] px-3 py-2 text-[#6d28d9] hover:bg-[#f0e9ff] focus-visible:ring-2 focus-visible:ring-[#8b5cf6]" onClick={report}><ClipboardCheck size={16} />用这些资料准备报告</button>}
    </details>
    {materials.length < ids.length && <p role="status" className="mt-2">{ids.length - materials.length} 项资料已移除或不属于当前账号。</p>}
    {error && <p role="alert" className="mt-2 text-[#a02b47]">{error}</p>}
  </section>
}
