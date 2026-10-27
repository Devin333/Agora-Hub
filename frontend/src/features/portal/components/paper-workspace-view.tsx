"use client"

import { useEffect, useMemo, useState, type ReactNode } from "react"
import Link from "next/link"
import { RefreshCw, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { fetchPaperDetail } from "@/lib/papers/api"
import { formatPaperDate, paperPdfUrl, paperSnippet, paperTitle } from "@/lib/papers/format"
import { paperReaderHref, rememberPaperScroll } from "@/lib/papers/discovery-navigation"
import type { Locale, Paper } from "@/lib/papers/types"
import { usePaperWorkspaceStore } from "@/stores/paper-workspace-store"
import { PaperDiscoveryRow } from "./paper-discovery-row"

export function PaperWorkspaceView({ view, ids, catalog, locale, returnTo, onPreview, onDiscover }: {
  view: "reading" | "compare"; ids: string[]; catalog: Paper[]; locale: Locale; returnTo: string;
  onPreview: (paper: Paper) => void; onDiscover: () => void
}) {
  const zh = locale === "zh"
  const [resolved, setResolved] = useState<Record<string, Paper>>({})
  const [errors, setErrors] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [retry, setRetry] = useState(0)
  const remove = usePaperWorkspaceStore((state) => state.removePaper)
  const catalogById = useMemo(() => new Map(catalog.filter((paper) => paper.isPublished !== false).map((paper) => [paper.id, paper])), [catalog])
  const identity = ids.join("\n")

  useEffect(() => {
    let cancelled = false
    const missing = identity.split("\n").filter((id) => id && !catalogById.has(id))
    setErrors([])
    setLoading(missing.length > 0)
    // Small bounded batches avoid flooding the detail API for an old reading list.
    void (async () => {
      const next: Record<string, Paper> = {}
      const failures: string[] = []
      for (let index = 0; index < missing.length && !cancelled; index += 4) {
        await Promise.all(missing.slice(index, index + 4).map(async (id) => {
          try {
            const paper = await fetchPaperDetail(id)
            if (!paper || paper.isPublished === false) throw new Error("unavailable")
            next[id] = paper
          } catch { failures.push(id) }
        }))
      }
      if (!cancelled) { setResolved(next); setErrors(failures); setLoading(false) }
    })()
    return () => { cancelled = true }
  }, [identity, catalogById, retry])

  function removeSelection(id: string) {
    remove(view === "reading" ? "readingList" : "compare", id)
    if (view === "reading") remove("later", id)
  }
  const papers = ids.map((id) => catalogById.get(id) ?? resolved[id]).filter((paper): paper is Paper => Boolean(paper))

  return <section className="min-w-0" aria-busy={loading} aria-labelledby="local-workspace-title">
    <div className="mb-3 flex min-h-9 items-center justify-between gap-4">
      <div className="flex flex-wrap items-baseline gap-3"><h2 id="local-workspace-title" className="text-heading3 font-semibold">{view === "reading" ? (zh ? "阅读列表" : "Reading list") : (zh ? "论文对比" : "Compare papers")}</h2><span className="text-body2 text-muted-foreground">{ids.length} {zh ? "篇" : "papers"}</span></div>
      <span className="text-label text-faint-foreground">{zh ? "保存在当前浏览器" : "Saved in this browser"}</span>
    </div>
    {loading && <p role="status" className="py-3 text-body2 text-muted-foreground">{zh ? "正在读取已保存的论文..." : "Loading saved papers..."}</p>}
    {!!errors.length && <div className="mb-3 divide-y divide-warning/20 rounded-xl border border-warning/30 bg-warning/10 text-body2 text-warning-text" role="status">
      <div className="flex items-center justify-between gap-3 px-4 py-3"><p>{zh ? "部分论文暂时无法读取，选择记录已保留。" : "Some papers are unavailable. Your selections are retained."}</p><Button type="button" variant="ghost" size="sm" className="text-warning-text hover:bg-warning/15 hover:text-warning-text" onClick={() => setRetry((value) => value + 1)}><RefreshCw size={15} strokeWidth={1.8} />{zh ? "重试" : "Retry"}</Button></div>
      {errors.map((id) => <div key={id} className="flex items-center justify-between gap-3 px-4 py-2"><span className="break-all font-mono text-label">{id}</span><Button type="button" variant="ghost" size="icon-sm" className="text-warning-text hover:bg-warning/15 hover:text-warning-text" aria-label={`${zh ? "移除" : "Remove"} ${id}`} onClick={() => removeSelection(id)}><X size={15} strokeWidth={1.8} /></Button></div>)}
    </div>}
    {!ids.length && <div className="py-16 text-center text-body2 text-muted-foreground"><h3 className="text-body1 font-medium text-foreground">{zh ? (view === "reading" ? "还没有收藏论文" : "还没有选择对比论文") : "No papers selected"}</h3><Button type="button" variant="link" className="mt-1 text-body2" onClick={onDiscover}>{zh ? "发现论文" : "Discover papers"}</Button></div>}
    {view === "reading" ? (papers.length > 0 && <div className="divide-y rounded-xl border">{papers.map((paper, index) => <PaperDiscoveryRow key={paper.id} paper={paper} locale={locale} returnTo={returnTo} onPreview={onPreview} renderPdfPreview={index < 3} />)}</div>) : <>
      {papers.length === 1 && <p className="flex flex-wrap items-center gap-2 py-3 text-body2 text-muted-foreground">{zh ? "再选一篇论文，即可并排对比。" : "Select one more paper to compare."}<Button type="button" variant="link" className="h-auto px-0 text-body2" onClick={onDiscover}>{zh ? "继续选论文" : "Find another paper"}</Button></p>}
      {!!papers.length && <div className="rk-scroll overflow-x-auto rounded-xl border outline-none focus-visible:ring-3 focus-visible:ring-ring/50" tabIndex={0} aria-label={zh ? "论文对比表" : "Paper comparison table"}><table className="w-full table-fixed border-collapse text-body2 [&_td]:w-[310px] [&_td]:min-w-[310px] [&_td]:break-words [&_td]:border-b [&_td]:border-r [&_td]:px-4 [&_td]:py-3 [&_td]:align-top [&_td]:text-muted-foreground [&_th]:break-words [&_th]:border-b [&_th]:border-r [&_th]:px-4 [&_th]:py-3 [&_th]:text-left [&_th]:align-top [&_tr>*:last-child]:border-r-0 [&_tbody_tr:last-child>*]:border-b-0">
        <thead className="bg-muted/50"><tr><th scope="col" className="!w-[120px] !min-w-[120px] font-medium text-muted-foreground">{zh ? "对比项" : "Field"}</th>{papers.map((paper) => <th key={paper.id} scope="col" className="relative w-[310px] min-w-[310px]"><Button type="button" variant="ghost" size="icon-xs" className="absolute end-2 top-2 text-muted-foreground" aria-label={`${zh ? "移出对比" : "Remove"} ${paper.title}`} onClick={() => removeSelection(paper.id)}><X size={15} strokeWidth={1.8} /></Button><button type="button" className="block pe-6 text-left text-body1 font-semibold text-foreground underline-offset-4 outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50" onClick={() => onPreview(paper)}>{paperTitle(paper, locale)}</button><Link className="mt-2 inline-block text-label font-medium text-link underline-offset-4 hover:underline" href={paperReaderHref(paper.slug || paper.id, returnTo)} onClick={() => rememberPaperScroll(returnTo)}>{zh ? "阅读全文" : "Read paper"}</Link></th>)}</tr></thead>
        <tbody>
          <CompareField title={zh ? "作者" : "Authors"} papers={papers} render={(paper) => paper.authors.join(", ")} />
          <CompareField title={zh ? "发布时间" : "Published"} papers={papers} render={(paper) => formatPaperDate(paper.publishedAt, locale)} />
          <CompareField title={zh ? "研究领域" : "Topics"} papers={papers} render={(paper) => [...new Set(paper.tags)].join(", ")} />
          <CompareField title={zh ? "研究方法" : "Methods"} papers={papers} render={(paper) => paper.methodRefs.map((ref) => zh ? ref.nameZh || ref.name : ref.name).join(", ")} />
          <CompareField title={zh ? "研究任务" : "Tasks"} papers={papers} render={(paper) => paper.taskRefs.map((ref) => zh ? ref.nameZh || ref.name : ref.name).join(", ")} />
          <CompareField title={zh ? "引用数" : "Citations"} papers={papers} render={(paper) => paper.citationCount?.toLocaleString()} />
          <CompareField title={zh ? "评测记录数" : "Benchmark records"} papers={papers} render={(paper) => paper.benchmarks?.length} />
          <CompareField title={zh ? "来源" : "Sources"} papers={papers} render={(paper) => {
            const pdf = paperPdfUrl(paper)
            const repo = paper.repoUrl && /^https:\/\/github\.com\/[^/]+\/[^/]+/.test(paper.repoUrl) ? paper.repoUrl : null
            return pdf || repo ? <span className="flex flex-wrap gap-3">{pdf && <a className="font-medium text-link underline-offset-4 hover:underline" href={pdf} target="_blank" rel="noreferrer">PDF</a>}{repo && <a className="font-medium text-link underline-offset-4 hover:underline" href={repo} target="_blank" rel="noreferrer">GitHub</a>}</span> : null
          }} />
          <CompareField title={zh ? "摘要" : "Abstract"} papers={papers} render={(paper) => paperSnippet(paper, locale)} />
        </tbody>
      </table></div>}
    </>}
  </section>
}

function CompareField({ title, papers, render }: { title: string; papers: Paper[]; render: (paper: Paper) => ReactNode }) {
  return <tr><th scope="row" className="font-medium text-muted-foreground">{title}</th>{papers.map((paper) => {
    const value = render(paper)
    return <td key={paper.id}>{value === undefined || value === null || value === "" ? <span aria-label="Unavailable" className="text-faint-foreground">—</span> : value}</td>
  })}</tr>
}
