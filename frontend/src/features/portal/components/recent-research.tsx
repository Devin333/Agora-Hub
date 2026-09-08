"use client"

import { Clock3, Trash2 } from "lucide-react"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { moduleInfo } from "@/lib/research/entry"
import { removeResearchVisit, type ResearchVisit } from "@/lib/research/history"

export function RecentResearch({ visits, onResume }: { visits: ResearchVisit[]; onResume: (visit: ResearchVisit) => void }) {
  return <DropdownMenu modal={false}><DropdownMenuTrigger className="inline-flex items-center gap-2 rounded-md hover:text-[#6735d3] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#8b5cf6] focus-visible:ring-offset-2"><Clock3 size={16} />最近研究</DropdownMenuTrigger>
    <DropdownMenuContent align="center" className="max-h-[420px] w-[440px] overflow-y-auto rounded-2xl border-[#e4d9ef] bg-white p-3 text-[#3f3158]">
      <p className="px-3 py-2 text-sm text-[#766885]">本机最近研究 · 最多 10 条</p>
      {visits.length === 0 ? <p className="px-3 py-5 text-[15px] text-[#766885]">开始一次研究后，可以在这里继续。</p> : visits.map(visit => <div key={visit.id} className="flex items-center gap-1"><DropdownMenuItem onSelect={() => onResume(visit)} className="min-w-0 flex-1 cursor-pointer rounded-xl px-3 py-3 focus:bg-[#f3edff]"><span className="block truncate text-[15px] font-medium">{visit.question}</span><span className="mt-1 block text-sm text-[#766885]">{moduleInfo[visit.module].name} · {new Date(visit.updatedAt).toLocaleDateString("zh-CN")}</span></DropdownMenuItem><DropdownMenuItem aria-label={`删除记录：${visit.question}`} onSelect={event => { event.preventDefault(); removeResearchVisit(visit.id) }} className="flex size-10 cursor-pointer items-center justify-center rounded-lg text-[#837491] focus:bg-[#f3edff]"><Trash2 size={16} /></DropdownMenuItem></div>)}
      {visits.length > 0 && <DropdownMenuItem onSelect={event => { event.preventDefault(); removeResearchVisit() }} className="mt-2 cursor-pointer rounded-lg border-t border-[#eee8f5] px-3 py-3 text-sm text-[#9a3450] focus:bg-[#fff4f7]">清空本机记录</DropdownMenuItem>}
    </DropdownMenuContent>
  </DropdownMenu>
}
