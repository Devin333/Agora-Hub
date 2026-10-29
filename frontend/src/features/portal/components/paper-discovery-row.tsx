"use client"

import Link from "next/link"
import { BookMarked, BookOpen, Check, FileText, Github, GitCompareArrows, Quote } from "lucide-react"
import { PaperThumbnail } from "@/components/papers/paper-thumbnail"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { formatPaperDate, paperPdfUrl, paperSnippet, paperTitle } from "@/lib/papers/format"
import { paperReaderHref, rememberPaperScroll } from "@/lib/papers/discovery-navigation"
import type { Locale, Paper } from "@/lib/papers/types"
import { usePaperWorkspaceStore } from "@/stores/paper-workspace-store"

export function PaperDiscoveryRow({ paper, locale, returnTo, onPreview, renderPdfPreview = false }: {
  paper: Paper; locale: Locale; returnTo: string; onPreview: (paper: Paper) => void; renderPdfPreview?: boolean
}) {
  const zh = locale === "zh"
  const saved = usePaperWorkspaceStore((state) => state.readingList.includes(paper.id) || state.later.includes(paper.id))
  const compared = usePaperWorkspaceStore((state) => state.compare.includes(paper.id))
  const toggleSaved = usePaperWorkspaceStore((state) => state.toggleSaved)
  const togglePaper = usePaperWorkspaceStore((state) => state.togglePaper)
  const pdf = paperPdfUrl(paper)
  const code = paper.repoUrl?.match(/^https:\/\/github\.com\/[^/]+\/[^/]+/) ? paper.repoUrl : undefined
  const title = paperTitle(paper, locale)

  return <article className="flex gap-4 px-4 py-4 transition-colors hover:bg-accent/40" data-testid="paper-row">
    <button type="button" className="h-[121px] w-[88px] shrink-0 self-start overflow-hidden rounded-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50" onClick={() => onPreview(paper)} aria-label={`${zh ? "预览封面" : "Preview cover"} ${title}`}>
      {/* The shared thumbnail renders at 128x176; scale it to the 88px list cover. */}
      <div className="origin-top-left scale-[0.6875]"><PaperThumbnail paper={paper} locale={locale} renderPdfPreview={renderPdfPreview} /></div>
    </button>
    <div className="min-w-0 flex-1">
      <h2><button type="button" className="block w-full break-words rounded-sm text-left text-body1 font-semibold text-foreground underline-offset-4 outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50" onClick={() => onPreview(paper)} aria-label={`${zh ? "预览" : "Preview"} ${title}`}>{title}</button></h2>
      <p className="mt-1 text-label text-muted-foreground">{paper.authors.slice(0, 3).join(", ")}<span aria-hidden="true"> · </span>{formatPaperDate(paper.publishedAt, locale)}{paper.venue ? ` · ${paper.venue}` : ""}</p>
      <p className="mt-1.5 line-clamp-2 text-body2 text-muted-foreground" data-paper-field="abstract">{paperSnippet(paper, locale)}</p>
      <div className="mt-2 flex flex-wrap gap-1.5">{[...new Set(paper.tags)].slice(0, 4).map((tag) => <Badge key={tag} variant="muted">{tag}</Badge>)}</div>
      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        <Button asChild size="sm" className="rounded-full px-3"><Link href={paperReaderHref(paper.slug || paper.id, returnTo)} onClick={() => rememberPaperScroll(returnTo)} aria-label={`${zh ? "阅读" : "Read"} ${title}`}><BookOpen size={15} strokeWidth={1.8} />{zh ? "阅读" : "Read"}</Link></Button>
        <Button type="button" variant="outline" size="sm" className="rounded-full px-3 aria-pressed:bg-accent" aria-pressed={saved} aria-label={`${saved ? (zh ? "取消收藏" : "Unsave") : (zh ? "收藏" : "Save")} ${title}`} onClick={() => toggleSaved(paper.id)}>{saved ? <Check size={15} strokeWidth={1.8} /> : <BookMarked size={15} strokeWidth={1.8} />}{saved ? (zh ? "已收藏" : "Saved") : (zh ? "收藏" : "Save")}</Button>
        {pdf && <Button asChild variant="ghost" size="sm" className="rounded-full px-3 text-muted-foreground"><a href={pdf} target="_blank" rel="noreferrer" aria-label={zh ? "打开论文 PDF" : "Open paper PDF"}><FileText size={15} strokeWidth={1.8} />PDF</a></Button>}
        {code && <Button asChild variant="ghost" size="sm" className="rounded-full px-3 text-muted-foreground"><a href={code} target="_blank" rel="noreferrer" aria-label={zh ? "打开代码仓库" : "Open code repository"}><Github size={15} strokeWidth={1.8} />{zh ? "代码" : "Code"}</a></Button>}
        <Button type="button" variant="ghost" size="sm" className="rounded-full px-3 text-muted-foreground aria-pressed:bg-accent aria-pressed:text-foreground" aria-pressed={compared} aria-label={`${compared ? (zh ? "移出对比" : "Remove from comparison") : (zh ? "加入对比" : "Compare")} ${title}`} onClick={() => togglePaper("compare", paper.id)}><GitCompareArrows size={15} strokeWidth={1.8} />{compared ? (zh ? "已选对比" : "Selected") : (zh ? "对比" : "Compare")}</Button>
        {typeof paper.citationCount === "number" && <span className="ms-auto inline-flex items-center gap-1 text-label tabular-nums text-faint-foreground" title={zh ? "OpenAlex 引用数" : "OpenAlex citations"}><Quote size={13} strokeWidth={1.8} />{paper.citationCount.toLocaleString()} {zh ? "引用" : "citations"}</span>}
      </div>
    </div>
  </article>
}
