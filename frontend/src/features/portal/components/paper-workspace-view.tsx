"use client"

import { useEffect, useMemo, useState, type ReactNode } from "react"
import Link from "next/link"
import { BookMarked, GitCompareArrows, RefreshCw, X } from "lucide-react"
import { fetchPaperDetail } from "@/lib/papers/api"
import { formatPaperDate, paperPdfUrl, paperSnippet, paperTitle } from "@/lib/papers/format"
import { paperReaderHref, rememberPaperScroll } from "@/lib/papers/discovery-navigation"
import type { Locale, Paper } from "@/lib/papers/types"
import { usePaperWorkspaceStore } from "@/stores/paper-workspace-store"
import { PaperDiscoveryRow } from "./paper-discovery-row"
import styles from "./papers-design-demo.module.css"

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

  return <section className={styles.localWorkspace} aria-busy={loading} aria-labelledby="local-workspace-title">
    <div className={styles.resultsHeader}>
      <div className={styles.resultHeading}><h2 id="local-workspace-title">{view === "reading" ? (zh ? "阅读列表" : "Reading list") : (zh ? "论文对比" : "Compare papers")}</h2><span>{ids.length} {zh ? "篇" : "papers"}</span></div>
      <span className={styles.localNote}>{zh ? "保存在当前浏览器" : "Saved in this browser"}</span>
    </div>
    {loading && <p role="status" className={styles.workspaceStatus}>{zh ? "正在读取已保存的论文..." : "Loading saved papers..."}</p>}
    {!!errors.length && <div className={styles.unavailable} role="status"><p>{zh ? "部分论文暂时无法读取，选择记录已保留。" : "Some papers are unavailable. Your selections are retained."}</p><button type="button" onClick={() => setRetry((value) => value + 1)}><RefreshCw size={15} />{zh ? "重试" : "Retry"}</button>{errors.map((id) => <div key={id}><span>{id}</span><button type="button" aria-label={`${zh ? "移除" : "Remove"} ${id}`} onClick={() => removeSelection(id)}><X size={15} /></button></div>)}</div>}
    {!ids.length && <div className={styles.empty}>{view === "reading" ? <BookMarked size={32} /> : <GitCompareArrows size={32} />}<h3>{zh ? (view === "reading" ? "还没有收藏论文" : "还没有选择对比论文") : "No papers selected"}</h3><button type="button" onClick={onDiscover}>{zh ? "发现论文" : "Discover papers"}</button></div>}
    {view === "reading" ? <div className={styles.paperList}>{papers.map((paper, index) => <PaperDiscoveryRow key={paper.id} paper={paper} locale={locale} returnTo={returnTo} onPreview={onPreview} renderPdfPreview={index < 3} />)}</div> : <>
      {papers.length === 1 && <p className={styles.workspaceStatus}>{zh ? "再选一篇论文，即可并排对比。" : "Select one more paper to compare."}<button type="button" onClick={onDiscover}>{zh ? "继续选论文" : "Find another paper"}</button></p>}
      {!!papers.length && <div className={styles.comparisonScroll} tabIndex={0} aria-label={zh ? "论文对比表" : "Paper comparison table"}><table className={styles.comparisonTable}>
        <thead><tr><th scope="col">{zh ? "对比项" : "Field"}</th>{papers.map((paper) => <th key={paper.id} scope="col"><button type="button" className={styles.removeComparison} aria-label={`${zh ? "移出对比" : "Remove"} ${paper.title}`} onClick={() => removeSelection(paper.id)}><X size={17} /></button><button type="button" className={styles.compareTitle} onClick={() => onPreview(paper)}>{paperTitle(paper, locale)}</button><Link href={paperReaderHref(paper.slug || paper.id, returnTo)} onClick={() => rememberPaperScroll(returnTo)}>{zh ? "阅读全文" : "Read paper"}</Link></th>)}</tr></thead>
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
            return pdf || repo ? <>{pdf && <a href={pdf} target="_blank" rel="noreferrer">PDF</a>}{repo && <a href={repo} target="_blank" rel="noreferrer">GitHub</a>}</> : null
          }} />
          <CompareField title={zh ? "摘要" : "Abstract"} papers={papers} render={(paper) => paperSnippet(paper, locale)} />
        </tbody>
      </table></div>}
    </>}
  </section>
}

function CompareField({ title, papers, render }: { title: string; papers: Paper[]; render: (paper: Paper) => ReactNode }) {
  return <tr><th scope="row">{title}</th>{papers.map((paper) => {
    const value = render(paper)
    return <td key={paper.id}>{value === undefined || value === null || value === "" ? <span aria-label="Unavailable">—</span> : value}</td>
  })}</tr>
}
