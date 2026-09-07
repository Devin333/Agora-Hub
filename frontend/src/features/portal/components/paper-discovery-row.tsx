"use client"

import Link from "next/link"
import { BookMarked, BookOpen, Check, FileText, Github, GitCompareArrows, Quote } from "lucide-react"
import { PaperThumbnail } from "@/components/papers/paper-thumbnail"
import { formatPaperDate, paperPdfUrl, paperSnippet, paperTitle } from "@/lib/papers/format"
import { paperReaderHref, rememberPaperScroll } from "@/lib/papers/discovery-navigation"
import type { Locale, Paper } from "@/lib/papers/types"
import { usePaperWorkspaceStore } from "@/stores/paper-workspace-store"
import styles from "./papers-design-demo.module.css"

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

  return <article className={styles.discoveryRow} data-testid="paper-row">
    <button type="button" className={styles.cover} onClick={() => onPreview(paper)} aria-label={`${zh ? "预览封面" : "Preview cover"} ${title}`}>
      <PaperThumbnail paper={paper} locale={locale} renderPdfPreview={renderPdfPreview} />
    </button>
    <div className={styles.paperBody}>
      <h2><button type="button" className={styles.paperTitle} onClick={() => onPreview(paper)} aria-label={`${zh ? "预览" : "Preview"} ${title}`}>{title}</button></h2>
      <p className={styles.paperMeta}>{paper.authors.slice(0, 3).join(", ")}<span aria-hidden="true"> · </span>{formatPaperDate(paper.publishedAt, locale)}{paper.venue ? ` · ${paper.venue}` : ""}</p>
      <p className={styles.paperAbstract} data-paper-field="abstract">{paperSnippet(paper, locale)}</p>
      <div className={styles.paperTags}>{[...new Set(paper.tags)].slice(0, 4).map((tag) => <span key={tag}>{tag}</span>)}</div>
      <div className={styles.paperActions}>
        <Link href={paperReaderHref(paper.slug || paper.id, returnTo)} onClick={() => rememberPaperScroll(returnTo)} className={styles.readAction} aria-label={`${zh ? "阅读" : "Read"} ${title}`}><BookOpen size={16} />{zh ? "阅读" : "Read"}</Link>
        <button type="button" aria-pressed={saved} aria-label={`${saved ? (zh ? "取消收藏" : "Unsave") : (zh ? "收藏" : "Save")} ${title}`} onClick={() => toggleSaved(paper.id)}>{saved ? <Check size={16} /> : <BookMarked size={16} />}{saved ? (zh ? "已收藏" : "Saved") : (zh ? "收藏" : "Save")}</button>
        {pdf && <a href={pdf} target="_blank" rel="noreferrer" aria-label={zh ? "打开论文 PDF" : "Open paper PDF"}><FileText size={15} />PDF</a>}
        {code && <a href={code} target="_blank" rel="noreferrer" aria-label={zh ? "打开代码仓库" : "Open code repository"}><Github size={15} />{zh ? "代码" : "Code"}</a>}
        <button type="button" aria-pressed={compared} aria-label={`${compared ? (zh ? "移出对比" : "Remove from comparison") : (zh ? "加入对比" : "Compare")} ${title}`} onClick={() => togglePaper("compare", paper.id)}><GitCompareArrows size={16} />{compared ? (zh ? "已选对比" : "Selected") : (zh ? "对比" : "Compare")}</button>
        {typeof paper.citationCount === "number" && <span className={styles.citations} title={zh ? "OpenAlex 引用数" : "OpenAlex citations"}><Quote size={14} />{paper.citationCount.toLocaleString()} {zh ? "引用" : "citations"}</span>}
      </div>
    </div>
  </article>
}
