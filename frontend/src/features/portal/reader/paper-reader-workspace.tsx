"use client"

import Link from "next/link"
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react"
import { ArrowLeft, ArrowUpRight, BookMarked, BookOpen, Check, ChevronRight, FileText, Focus, Github, List, LoaderCircle, Minus, Plus, RefreshCw, Sparkles } from "lucide-react"
import { OpenReaderPage } from "@/components/papers/open-reader"
import { PaperPdfViewer } from "@/components/papers/shared/paper-pdf-viewer"
import { apiGet } from "@/lib/api/client"
import { buildReaderParagraphs, buildReaderToc } from "@/components/papers/open-reader/open-reader-utils"
import { readerUnavailableMessage, type ReaderWorkspacePayload } from "./reader-contract"
import { formatPaperDate, paperPdfUrl } from "@/lib/papers/format"
import { usePaperWorkspaceStore } from "@/stores/paper-workspace-store"
import { ReaderAssistant, safeReaderSourceUrl } from "./reader-assistant"
import { useReaderNotebook } from "./use-reader-notebook"
import styles from "./reader-workspace.module.css"

export function PaperReaderWorkspace({ payload, backHref }: { payload: ReaderWorkspacePayload; backHref: string }) {
  const [current, setCurrent] = useState(payload)
  const [view, setView] = useState<"pdf" | "body" | "overview">("body")
  const [pdfAttempt, setPdfAttempt] = useState(0)
  const [focus, setFocus] = useState(false)
  const [busy, setBusy] = useState(false)
  const [statusError, setStatusError] = useState("")
  const [activeSection, setActiveSection] = useState("")
  const [sectionRequest, setSectionRequest] = useState<{ id: string; sequence: number }>()
  const [progress, setProgress] = useState(0)
  const operation = useRef<AbortController | null>(null)
  const paper = current.paper
  const notebook = useReaderNotebook(paper.id)
  const compiled = current.state === "ready" && Boolean(current.reader)
  const outline = useMemo(() => current.visualLayer?.outline?.length ? current.visualLayer.outline : current.reader ? buildReaderToc(buildReaderParagraphs(current.reader, "zh")) : [], [current.reader, current.visualLayer])
  const sectionIds = new Set(outline.map((section) => section.id))
  const saved = usePaperWorkspaceStore((state) => state.readingList.includes(paper.id) || state.later.includes(paper.id))
  const toggleSaved = usePaperWorkspaceStore((state) => state.toggleSaved)
  const pdf = safeReaderSourceUrl(paperPdfUrl(paper))
  const source = safeReaderSourceUrl(current.sourceUrl || paper.paperUrl || paper.arxivUrl)
  const repository = safeReaderSourceUrl(paper.repoUrl)
  const researchQuestion = new URL(backHref, "https://agora.invalid").searchParams.get("question") || ""

  useEffect(() => () => operation.current?.abort(), [])

  useEffect(() => {
    if (view !== "body") { setProgress(0); return }
    let frame = 0
    function update() {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        const body = document.querySelector<HTMLElement>("[data-open-reader-body]")
        if (!body) return
        const bounds = body.getBoundingClientRect()
        const readableHeight = Math.max(1, bounds.height - window.innerHeight + 150)
        setProgress(Math.round(Math.max(0, Math.min(100, (150 - bounds.top) / readableHeight * 100))))
      })
    }
    update()
    window.addEventListener("scroll", update, { passive: true })
    window.addEventListener("resize", update)
    return () => { cancelAnimationFrame(frame); window.removeEventListener("scroll", update); window.removeEventListener("resize", update) }
  }, [view, compiled])

  const { update: updateNotebook } = notebook
  const rememberSection = useCallback((id: string) => {
    setActiveSection(id)
    updateNotebook({ sectionId: id })
  }, [updateNotebook])
  const rememberPdfPage = useCallback((page: number) => updateNotebook({ pdfPage: page }), [updateNotebook])

  function navigate(id: string) {
    setView("body")
    setSectionRequest((previous) => ({ id, sequence: (previous?.sequence ?? 0) + 1 }))
  }

  async function refreshDocument() {
    if (busy) return
    setBusy(true)
    setStatusError("")
    const controller = new AbortController()
    operation.current = controller
    const timeout = window.setTimeout(() => controller.abort(), 30000)
    try {
      const result = await apiGet<{ success: boolean; data?: ReaderWorkspacePayload }>(`/api/papers/${encodeURIComponent(paper.id)}/workspace`, { signal: controller.signal })
      if (!result.success || !result.data) throw new Error("reader_unavailable")
      if (!controller.signal.aborted) {
        setCurrent(result.data)
        if (result.data.reader) setView("body")
      }
    } catch {
      if (!controller.signal.aborted) setStatusError("刷新失败，请稍后重试。")
      else setStatusError("请求等待超时，请刷新状态后再试。")
    } finally {
      clearTimeout(timeout)
      if (operation.current === controller) { operation.current = null; setBusy(false) }
    }
  }

  return <div className={styles.workspace} style={{ "--workspace-font-size": `${notebook.fontSize}px` } as CSSProperties}>
    <header className={styles.topbar}>
      <div className={styles.topbarLeft}><Link href={backHref} className={styles.back}><ArrowLeft size={18} />返回论文</Link><span className={styles.divider} /><Link href="/design-demo" className={styles.brand}><span><Sparkles size={19} /></span>Agora<span>AI</span></Link><span className={styles.readerLabel}>论文阅读</span></div>
      <div className={styles.topbarActions}>
        <button type="button" aria-pressed={saved} onClick={() => toggleSaved(paper.id)}>{saved ? <Check size={17} /> : <BookMarked size={17} />}{saved ? "已收藏" : "收藏论文"}</button>
        {pdf && <a className={styles.pdfAction} href={pdf} target="_blank" rel="noreferrer"><FileText size={16} />原文 PDF<ArrowUpRight size={15} /></a>}
      </div>
    </header>
    <div className={`${styles.layout} ${view === "pdf" ? styles.pdfLayout : ""} ${focus ? styles.focusLayout : ""}`}>
      <aside className={styles.outline} aria-label="论文目录" hidden={focus || view === "pdf"}>
        <div className={styles.outlineTitle}><List size={17} /><strong>论文目录</strong></div>
        <button type="button" className={styles.overviewLink} aria-current={view === "overview" ? "page" : undefined} onClick={() => { setView("overview"); window.scrollTo({ top: 0, behavior: "instant" }) }}><FileText size={16} />论文概览</button>
        {outline.length ? <nav aria-label="章节导航">{outline.map((section) => <button type="button" key={section.id} aria-current={view === "body" && section.id === activeSection ? "location" : undefined} data-depth={section.level > 1 ? "nested" : "root"} onClick={() => navigate(section.id)}>{section.sectionNumber && <span>{section.sectionNumber}</span>}{section.title}</button>)}</nav> : <p className={styles.outlineEmpty}>正文准备好后，章节目录会出现在这里。</p>}
        {notebook.resumeSection && sectionIds.has(notebook.resumeSection) && <button type="button" className={styles.resume} onClick={() => navigate(notebook.resumeSection)}>继续上次章节<ChevronRight size={15} /></button>}
        <div className={styles.readingProgress}><div><span>{view === "body" ? "阅读进度" : "当前阅读"}</span><strong>{view === "body" ? `${progress}%` : "概览"}</strong></div>{view === "body" && <progress max={100} value={progress} aria-label="阅读进度" />}</div>
        <div className={styles.readingTip}><BookOpen size={18} /><p>读到不明白的地方？<br />选中正文，解释或记下疑问。</p></div>
      </aside>
      <div className={styles.center}>
        <div className={styles.readerToolbar}>
          <div className={styles.viewSwitch} role="group" aria-label="阅读视图"><button type="button" aria-pressed={view === "body"} onClick={() => setView("body")}>章节精读</button><button type="button" aria-pressed={view === "overview"} onClick={() => setView("overview")}>论文概览</button><button type="button" disabled={!pdf} aria-pressed={view === "pdf"} onClick={() => setView("pdf")}>PDF 对照</button></div>
          <div className={styles.readingControls}>{view !== "pdf" && <><button type="button" aria-label="缩小正文字号" disabled={!notebook.ready || notebook.fontSize <= 16} onClick={() => notebook.update({ fontSize: notebook.fontSize - 2 })}><Minus size={14} /></button><span aria-label="当前正文字号">{notebook.fontSize}</span><button type="button" aria-label="放大正文字号" disabled={!notebook.ready || notebook.fontSize >= 22} onClick={() => notebook.update({ fontSize: notebook.fontSize + 2 })}><Plus size={14} /></button><span className={styles.divider} /></>}<button type="button" aria-pressed={focus} onClick={() => setFocus(!focus)}><Focus size={16} />{focus ? "退出专注" : "专注"}</button></div>
        </div>
        <article className={styles.paper}>
          {view !== "pdf" && <header className={styles.paperHeading}>
            <div className={styles.paperEyebrow}><span>{paper.venue || "RESEARCH PAPER"}</span><span>·</span><span>{formatPaperDate(paper.publishedAt, "zh")}</span></div>
            <h1>{paper.title}</h1>
            <p className={styles.authors}>{paper.authors.join(", ")}</p>
            <div className={styles.paperLinks}>{source && <a href={source} target="_blank" rel="noreferrer">论文来源<ArrowUpRight size={14} /></a>}{repository && <a href={repository} target="_blank" rel="noreferrer"><Github size={15} />代码仓库<ArrowUpRight size={14} /></a>}{paper.tags.slice(0, 3).map((tag) => <span key={tag}>{tag}</span>)}</div>
          </header>}
          {view === "pdf" && pdf ? notebook.ready ? <PaperPdfViewer key={`${paper.id}:${pdfAttempt}`} pdfUrl={pdf} title={paper.title} locale="zh" paperId={paper.id} initialPage={notebook.pdfPage} onPageChange={rememberPdfPage} className={styles.pdfViewer} errorMessage="完整 PDF 暂时加载失败，请重试或打开论文原文。" fallback={<div className={styles.pdfError}><BookOpen size={28} /><h2>暂时无法加载 PDF</h2><div className={styles.statusActions}><button type="button" onClick={() => setPdfAttempt((attempt) => attempt + 1)}>重新加载 PDF</button><a href={pdf} target="_blank" rel="noreferrer">打开原文 PDF<ArrowUpRight size={14} /></a></div></div>} /> : <div className={styles.pdfError} role="status">正在打开原文 PDF…</div> : view === "body" && compiled && current.reader ? <OpenReaderPage reader={current.reader} locale="zh" backHref={backHref} visualLayer={current.visualLayer} workspace={{ fontSize: notebook.fontSize, sectionRequest, onSectionChange: rememberSection }} /> : <div className={styles.overviewBody}>
            {!compiled && <section className={styles.documentStatus} aria-label="正文状态"><div className={styles.statusHeading}><BookOpen size={19} /><strong>{current.state === "needs_review" ? "全文转换结果待校验" : "章节全文尚未就绪"}</strong></div><p>{readerUnavailableMessage(current.reasonCode)}</p><div className={styles.statusActions}>{source && <a href={source} target="_blank" rel="noreferrer">查看论文来源<ArrowUpRight size={14} /></a>}<button type="button" disabled={busy} aria-label="刷新正文状态" onClick={() => void refreshDocument()}>{busy ? <LoaderCircle size={14} className={styles.spinner} /> : <RefreshCw size={14} />}{busy ? "正在获取全文…" : "重新获取全文"}</button></div>{statusError && <p role="alert" className={styles.error}>{statusError}</p>}</section>}
            {view === "overview" && <><section className={styles.abstract}><div className={styles.sectionEyebrow}>ABSTRACT <span>原文摘要</span></div><h2>论文概览</h2><p>{paper.abstractSnippet || "当前没有公开摘要，请打开论文来源查看。"}</p></section>
            {compiled && <div className={styles.readNext}><BookOpen size={21} /><div><strong>继续阅读完整论文</strong></div><button type="button" onClick={() => setView("body")} aria-label="开始阅读正文"><ChevronRight size={20} /></button></div>}</>}
          </div>}
        </article>
        <p className={styles.articleFooter}>{view === "pdf" ? "完整论文原文 · 支持翻页、全文搜索和文本选择 · 自动记住阅读页码" : view === "body" && compiled ? "正文保留论文原始内容，AI 解读在右侧单独呈现。" : "摘要来自论文公开信息，完整内容以原文为准。"}</p>
      </div>
      <div hidden={focus} className={styles.assistantColumn}><ReaderAssistant paper={paper} notebook={notebook} sectionIds={sectionIds} onNavigate={navigate} researchQuestion={researchQuestion} unavailableMessage={compiled && current.aiReady !== false ? undefined : "AI 问答暂未就绪，不影响原文阅读和笔记。"} /></div>
    </div>
  </div>
}
