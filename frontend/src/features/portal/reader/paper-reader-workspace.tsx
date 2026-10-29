"use client"

import Link from "next/link"
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react"
import { ArrowLeft, ArrowUpRight, BookMarked, BookOpen, Check, ChevronRight, FileText, Focus, Github, List, LoaderCircle, Minus, Plus, RefreshCw } from "lucide-react"
import { OpenReaderPage } from "@/components/papers/open-reader"
import type { ReaderSelectionAskAnswer, ReaderSelectionAskDraft, ReaderSelectionAskRequest } from "@/components/papers/open-reader/open-reader-types"
import { PaperPdfViewer } from "@/components/papers/shared/paper-pdf-viewer"
import { BrandMark } from "@/components/brand/brand-mark"
import { apiGet } from "@/lib/api/client"
import { fetchDesktopReader } from "@/lib/research/desktop/client"
import { isHostedProfile } from "@/lib/deployment-profile"
import { buildReaderParagraphs, buildReaderToc } from "@/components/papers/open-reader/open-reader-utils"
import {
  hasOwnedAnalysisSource,
  needsOwnedSourcePreparation,
  readerUnavailableMessage,
  type ReaderWorkspacePayload,
} from "./reader-contract"
import { formatPaperDate, paperPdfUrl } from "@/lib/papers/format"
import type { PaperReaderCitation } from "@/lib/papers/types"
import { resolveCitationLocation, resolveRequestedReaderLocation } from "@/lib/research/reader-location"
import { usePaperWorkspaceStore } from "@/stores/paper-workspace-store"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import { matchesSelectionAnswerScope } from "@/lib/research/selection-ask"
import { researchHomeHref } from "@/lib/research/routes"
import { normalizeAnalyzeTransportUrl } from "@/lib/research/desktop/request-contracts"
import {
  historyState,
  rememberResearchActivity,
  removeResearchMaterial,
  saveResearchMaterial,
} from "@/lib/research/history"
import { ReaderAssistant, safeReaderSourceUrl } from "./reader-assistant"
import { ReaderAnalysisControl } from "./reader-analysis-control"
import { ReaderPreparationControl } from "./reader-preparation-control"
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
  const [analysisRunId, setAnalysisRunId] = useState(current.runId)
  const [selectionRequest, setSelectionRequest] = useState<ReaderSelectionAskRequest>()
  const [selectionAnswer, setSelectionAnswer] = useState<ReaderSelectionAskAnswer>()
  const operation = useRef<AbortController | null>(null)
  const workspace = useOwnedResearchWorkspace()
  const ownerRef = useRef(workspace.owner)
  ownerRef.current = workspace.owner
  const sourceRevisionRef = useRef(current.sourceRevision)
  sourceRevisionRef.current = current.sourceRevision
  const hostedProfile = isHostedProfile()
  const paper = current.paper
  const sourceRevision = current.sourceRevision ?? ""
  const sourceIdentityExact = Boolean(current.sourceRevision && current.sourceFingerprint)
  const canAnalyze = hasOwnedAnalysisSource(current)
  const needsPreparation = needsOwnedSourcePreparation(current, hostedProfile)
  const notebook = useReaderNotebook(paper.id, sourceRevision)
  const updateNotebook = notebook.update
  const notebookReady = notebook.ready
  const notebookPdfPage = notebook.pdfPage
  const compiled = current.state === "ready" && Boolean(current.reader)
  const outline = useMemo(() => current.visualLayer?.outline?.length ? current.visualLayer.outline : current.reader ? buildReaderToc(buildReaderParagraphs(current.reader, "zh")) : [], [current.reader, current.visualLayer])
  const sectionIds = new Set(outline.map((section) => section.id))
  const locallySaved = usePaperWorkspaceStore((state) => state.readingList.includes(paper.id) || state.later.includes(paper.id))
  const toggleLocalSaved = usePaperWorkspaceStore((state) => state.toggleSaved)
  const materialId = `paper:${paper.id.replace(/[^A-Za-z0-9:_-]/g, "-")}`
  const ownedMaterial = (workspace.workspace.materials ?? []).find(
    item => item.id === materialId && item.referenceId === paper.id,
  )
  const saved = hostedProfile ? Boolean(ownedMaterial) : locallySaved
  const pdf = safeReaderSourceUrl(paperPdfUrl(paper))
  const source = safeReaderSourceUrl(current.sourceUrl || paper.paperUrl || paper.arxivUrl)
  const repository = safeReaderSourceUrl(paper.repoUrl)
  const preparationSource = normalizeAnalyzeTransportUrl(source) ?? normalizeAnalyzeTransportUrl(pdf)
  const researchQuestion = new URL(backHref, "https://agora.invalid").searchParams.get("question") || ""
  const researchSession = new URL(backHref, "https://agora.invalid").searchParams.get("researchSession")

  useEffect(() => () => operation.current?.abort(), [])

  useEffect(() => {
    setSelectionRequest(undefined)
    setSelectionAnswer(undefined)
  }, [workspace.owner, paper.id, current.sourceRevision])

  const requestSelectionAsk = useCallback((draft: ReaderSelectionAskDraft) => {
    if (!workspace.owner || !current.sourceRevision || !sourceIdentityExact) return
    setSelectionAnswer(undefined)
    setSelectionRequest({
      ...draft,
      requestKey: crypto.randomUUID(),
      owner: workspace.owner,
      sourceRevision: current.sourceRevision,
    })
  }, [current.sourceRevision, sourceIdentityExact, workspace.owner])

  const receiveSelectionAnswer = useCallback((answer: ReaderSelectionAskAnswer) => {
    if (!matchesSelectionAnswerScope(
      answer,
      ownerRef.current,
      sourceRevisionRef.current,
      selectionRequest?.requestKey,
    )) return
    setSelectionAnswer(answer)
  }, [selectionRequest?.requestKey])

  useEffect(() => {
    if (!notebookReady) return
    const resolution = resolveRequestedReaderLocation(
      window.location.search,
      sourceIdentityExact ? current.sourceRevision : undefined,
      new Set(outline.map(section => section.id)),
      Boolean(pdf),
    )
    if (!resolution.allowed) {
      setActiveSection("")
      setSectionRequest(undefined)
      if (resolution.reason === "source_revision_mismatch") {
        setStatusError("保存的定位来自另一来源修订，本次已安全重置章节和 PDF 页码。")
      } else if (resolution.reason === "source_identity_unavailable") {
        setStatusError("来源修订身份尚未确认，本次没有恢复章节或 PDF 页码定位。")
      } else {
        setStatusError("保存的章节或 PDF 页码在当前来源修订中不可用。")
      }
      return
    }
    if (resolution.location.sectionId) {
      const sectionId = resolution.location.sectionId
      setActiveSection(sectionId)
      setSectionRequest(previous => ({
        id: sectionId,
        sequence: (previous?.sequence ?? 0) + 1,
      }))
    }
    if (resolution.location.page !== undefined) {
      updateNotebook({ pdfPage: resolution.location.page })
      setView("pdf")
    }
  }, [current.sourceRevision, notebookReady, outline, pdf, sourceIdentityExact, updateNotebook])

  useEffect(() => {
    if (!researchSession || !notebookReady || !historyState().ready) return
    const section = outline.find(item => item.id === activeSection)
    const activityHref = `${window.location.pathname}`
    rememberResearchActivity(researchSession, {
      kind: "reader",
      paperId: paper.id,
      title: paper.title,
      href: activityHref,
      ...(sourceIdentityExact && current.sourceRevision
        ? {
            sourceRevision: current.sourceRevision,
            ...(activeSection ? { sectionId: activeSection } : {}),
            ...(section?.title ? { sectionTitle: section.title } : {}),
            ...(view === "pdf" ? { pdfPage: notebookPdfPage } : {}),
          }
        : {}),
      updatedAt: Date.now(),
    }, historyState().owner)
  }, [activeSection, current.sourceRevision, notebookPdfPage, notebookReady, outline, paper.id, paper.title, researchSession, sourceIdentityExact, view])

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

  const rememberSection = useCallback((id: string) => {
    setActiveSection(id)
    updateNotebook({ sectionId: id })
  }, [updateNotebook])
  const rememberPdfPage = useCallback((page: number) => updateNotebook({ pdfPage: page }), [updateNotebook])

  function navigate(id: string) {
    setView("body")
    setSectionRequest((previous) => ({ id, sequence: (previous?.sequence ?? 0) + 1 }))
  }

  function navigateCitation(citation: PaperReaderCitation) {
    const resolution = resolveCitationLocation(
      citation,
      sourceIdentityExact ? current.sourceRevision : undefined,
      sectionIds,
      Boolean(pdf),
    )
    if (!resolution.allowed) {
      setStatusError(
        resolution.reason === "source_revision_mismatch"
          ? "该引用来自另一来源修订，未执行定位。"
          : "该引用在当前来源修订中没有可核对的精确定位。",
      )
      return
    }
    if (resolution.location.sectionId) navigate(resolution.location.sectionId)
    if (resolution.location.page !== undefined) {
      notebook.update({ pdfPage: resolution.location.page })
      setView("pdf")
    }
  }

  async function refreshDocument(expectedRunId?: string) {
    if (busy) return
    setBusy(true)
    setStatusError("")
    const controller = new AbortController()
    operation.current = controller
    const timeout = window.setTimeout(() => controller.abort(), 30000)
    try {
      let next: ReaderWorkspacePayload
      if (hostedProfile) {
        next = await fetchDesktopReader(paper.id, controller.signal)
        if (expectedRunId && next.runId !== expectedRunId) {
          throw new Error("新分析运行尚未成为当前可核对的阅读投影。")
        }
      } else {
        const result = await apiGet<{ success: boolean; data?: ReaderWorkspacePayload }>(
          `/api/papers/${encodeURIComponent(paper.id)}/workspace`,
          { signal: controller.signal },
        )
        if (!result.success || !result.data) throw new Error("reader_unavailable")
        next = result.data
      }
      if (!controller.signal.aborted) {
        setCurrent({ ...next, aiReady: next.capabilities?.ask ?? next.aiReady })
        setAnalysisRunId(next.runId)
        if (next.reader) setView("body")
      }
    } catch (cause) {
      if (!controller.signal.aborted) {
        setStatusError(cause instanceof Error ? cause.message : "刷新失败，请稍后重试。")
      } else {
        setStatusError("请求等待超时，请刷新状态后再试。")
      }
    } finally {
      clearTimeout(timeout)
      if (operation.current === controller) {
        operation.current = null
        setBusy(false)
      }
    }
  }

  function toggleSavedPaper() {
    if (!hostedProfile) {
      toggleLocalSaved(paper.id)
      return
    }
    if (!workspace.owner) {
      setStatusError("请先登录，再保存私有论文。")
      return
    }
    if (ownedMaterial) {
      if (!removeResearchMaterial(materialId, workspace.owner)) {
        setStatusError("未能从研究工作区移除收藏。")
      }
      return
    }
    const now = Date.now()
    const savedSuccessfully = saveResearchMaterial(
      {
        id: materialId,
        groupId: null,
        kind: "paper",
        title: paper.title,
        url: source ?? "",
        referenceId: paper.id,
        readerHref: sourceIdentityExact && current.sourceRevision
          ? `/papers/${encodeURIComponent(paper.id)}/read?sourceRevision=${encodeURIComponent(current.sourceRevision)}`
          : `/papers/${encodeURIComponent(paper.id)}/read`,
        notes: "",
        createdAt: now,
        updatedAt: now,
      },
      workspace.owner,
    )
    if (!savedSuccessfully) setStatusError("未能保存到研究工作区。")
  }

  return <div className={styles.workspace} style={{ "--workspace-font-size": `${notebook.fontSize}px` } as CSSProperties}>
    <header className={styles.topbar}>
      <div className={styles.topbarLeft}><Link href={backHref} className={styles.back}><ArrowLeft size={16} strokeWidth={1.8} />返回论文</Link><span className={styles.divider} /><Link href={researchHomeHref()} className={styles.brand}><BrandMark size={24} />Agora<span>AI</span></Link><span className={styles.readerLabel}>论文阅读</span></div>
      <div className={styles.topbarActions}>
        <button type="button" aria-pressed={saved} onClick={toggleSavedPaper}>{saved ? <Check size={16} strokeWidth={1.8} /> : <BookMarked size={16} strokeWidth={1.8} />}{saved ? "已收藏" : "收藏论文"}</button>
        {pdf && <a className={styles.pdfAction} href={pdf} target="_blank" rel="noreferrer"><FileText size={16} strokeWidth={1.8} />原文 PDF<ArrowUpRight size={15} strokeWidth={1.8} /></a>}
      </div>
    </header>
    <div className={`${styles.layout} ${view === "pdf" ? styles.pdfLayout : ""} ${focus ? styles.focusLayout : ""}`}>
      <aside className={styles.outline} aria-label="论文目录" hidden={focus || view === "pdf"}>
        <div className={styles.outlineTitle}><List size={16} strokeWidth={1.8} /><strong>论文目录</strong></div>
        <button type="button" className={styles.overviewLink} aria-current={view === "overview" ? "page" : undefined} onClick={() => { setView("overview"); window.scrollTo({ top: 0, behavior: "instant" }) }}><FileText size={16} strokeWidth={1.8} />论文概览</button>
        {outline.length ? <nav aria-label="章节导航">{outline.map((section) => <button type="button" key={section.id} aria-current={view === "body" && section.id === activeSection ? "location" : undefined} data-depth={section.level > 1 ? "nested" : "root"} onClick={() => navigate(section.id)}>{section.sectionNumber && <span>{section.sectionNumber}</span>}{section.title}</button>)}</nav> : <p className={styles.outlineEmpty}>正文准备好后，章节目录会出现在这里。</p>}
        {notebook.resumeSection && sectionIds.has(notebook.resumeSection) && <button type="button" className={styles.resume} onClick={() => navigate(notebook.resumeSection)}>继续上次章节<ChevronRight size={15} strokeWidth={1.8} /></button>}
        <div className={styles.readingProgress}><div><span>{view === "body" ? "阅读进度" : "当前阅读"}</span><strong>{view === "body" ? `${progress}%` : "概览"}</strong></div>{view === "body" && <progress max={100} value={progress} aria-label="阅读进度" />}</div>
        <div className={styles.readingTip}><BookOpen size={16} strokeWidth={1.8} /><p>读到不明白的地方？<br />选中正文，解释或记下疑问。</p></div>
      </aside>
      <div className={styles.center}>
        <div className={styles.readerToolbar}>
          <div className={styles.viewSwitch} role="group" aria-label="阅读视图"><button type="button" aria-pressed={view === "body"} onClick={() => setView("body")}>章节精读</button><button type="button" aria-pressed={view === "overview"} onClick={() => setView("overview")}>论文概览</button><button type="button" disabled={!pdf} aria-pressed={view === "pdf"} onClick={() => setView("pdf")}>PDF 对照</button></div>
          <div className={styles.readingControls}>{view !== "pdf" && <><button type="button" aria-label="缩小正文字号" disabled={!notebook.ready || notebook.fontSize <= 16} onClick={() => notebook.update({ fontSize: adjacentReaderFontSize(notebook.fontSize, -1) })}><Minus size={14} strokeWidth={1.8} /></button><span aria-label="当前正文字号">{notebook.fontSize}</span><button type="button" aria-label="放大正文字号" disabled={!notebook.ready || notebook.fontSize >= 22} onClick={() => notebook.update({ fontSize: adjacentReaderFontSize(notebook.fontSize, 1) })}><Plus size={14} strokeWidth={1.8} /></button><span className={styles.divider} /></>}<button type="button" aria-pressed={focus} onClick={() => setFocus(!focus)}><Focus size={16} strokeWidth={1.8} />{focus ? "退出专注" : "专注"}</button></div>
        </div>
        <article className={styles.paper}>
          {view !== "pdf" && <header className={styles.paperHeading}>
            <div className={styles.paperEyebrow}><span>{paper.venue || "RESEARCH PAPER"}</span><span>·</span><span>{formatPaperDate(paper.publishedAt, "zh")}</span></div>
            <h1>{paper.title}</h1>
            <p className={styles.authors}>{paper.authors.join(", ")}</p>
            <div className={styles.paperLinks}>{source && <a href={source} target="_blank" rel="noreferrer">论文来源<ArrowUpRight size={14} strokeWidth={1.8} /></a>}{repository && <a href={repository} target="_blank" rel="noreferrer"><Github size={15} strokeWidth={1.8} />代码仓库<ArrowUpRight size={14} strokeWidth={1.8} /></a>}{paper.tags.slice(0, 3).map((tag) => <span key={tag}>{tag}</span>)}</div>
          </header>}
          {view === "pdf" && pdf ? notebook.ready ? <PaperPdfViewer key={`${paper.id}:${pdfAttempt}`} pdfUrl={pdf} title={paper.title} locale="zh" paperId={paper.id} initialPage={notebook.pdfPage} onPageChange={rememberPdfPage} className={styles.pdfViewer} errorMessage="完整 PDF 暂时加载失败，请重试或打开论文原文。" fallback={<div className={styles.pdfError}><BookOpen size={28} strokeWidth={1.8} /><h2>暂时无法加载 PDF</h2><div className={styles.statusActions}><button type="button" onClick={() => setPdfAttempt((attempt) => attempt + 1)}>重新加载 PDF</button><a href={pdf} target="_blank" rel="noreferrer">打开原文 PDF<ArrowUpRight size={14} strokeWidth={1.8} /></a></div></div>} /> : <div className={styles.pdfError} role="status">正在打开原文 PDF…</div> : view === "body" && compiled && current.reader ? <OpenReaderPage reader={current.reader} locale="zh" backHref={backHref} visualLayer={current.visualLayer} workspace={{ fontSize: notebook.fontSize, sourceRevision: current.sourceRevision, sectionRequest, onSectionChange: rememberSection, onSelectionAsk: requestSelectionAsk, selectionAnswer, onSelectionAnswerApplied: requestKey => { if (selectionAnswer?.requestKey === requestKey) { setSelectionAnswer(undefined); setSelectionRequest(undefined) } } }} /> : <div className={styles.overviewBody}>
            {!compiled && <section className={styles.documentStatus} aria-label="正文状态"><div className={styles.statusHeading}><BookOpen size={16} strokeWidth={1.8} /><strong>{current.state === "needs_review" ? "全文转换结果待校验" : "章节全文尚未就绪"}</strong></div><p>{readerUnavailableMessage(current.reasonCode)}</p><div className={styles.statusActions}>{source && <a href={source} target="_blank" rel="noreferrer">查看论文来源<ArrowUpRight size={14} strokeWidth={1.8} /></a>}<button type="button" disabled={busy} aria-label="刷新正文状态" onClick={() => void refreshDocument()}>{busy ? <LoaderCircle size={14} className={styles.spinner} strokeWidth={1.8} /> : <RefreshCw size={14} strokeWidth={1.8} />}{busy ? "正在获取全文…" : "重新获取全文"}</button></div>{statusError && <p role="alert" className={styles.error}>{statusError}</p>}</section>}
            {view === "overview" && <><section className={styles.abstract}><div className={styles.sectionEyebrow}>ABSTRACT <span>原文摘要</span></div><h2>论文概览</h2><p>{paper.abstractSnippet || "当前没有公开摘要，请打开论文来源查看。"}</p></section>
            {compiled && <div className={styles.readNext}><BookOpen size={21} strokeWidth={1.8} /><div><strong>继续阅读完整论文</strong></div><button type="button" onClick={() => setView("body")} aria-label="开始阅读正文"><ChevronRight size={16} strokeWidth={1.8} /></button></div>}</>}
          </div>}
        </article>
        <p className={styles.articleFooter}>{view === "pdf" ? "完整论文原文 · 支持翻页、全文搜索和文本选择 · 自动记住阅读页码" : view === "body" && compiled ? "正文保留论文原始内容，AI 解读在右侧单独呈现。" : "摘要来自论文公开信息，完整内容以原文为准。"}</p>
      </div>
      <div hidden={focus} className={styles.assistantColumn}>
        {needsPreparation && (
          <ReaderPreparationControl
            paper={paper}
            sourceUrl={preparationSource ?? undefined}
            enabled={Boolean(workspace.owner && preparationSource)}
            onReady={() => refreshDocument()}
          />
        )}
        <ReaderAnalysisControl
          paper={paper}
          sourceRevision={sourceRevision}
          sourceFingerprint={current.sourceFingerprint}
          sourceUrl={source}
          pdfUrl={pdf}
          initialRunId={analysisRunId}
          enabled={canAnalyze}
          onRun={runId => refreshDocument(runId)}
        />
        <ReaderAssistant
          paper={paper}
          notebook={notebook}
          sectionIds={sectionIds}
          onNavigateCitation={navigateCitation}
          researchQuestion={researchQuestion}
          sourceRevision={sourceRevision}
          sourceFingerprint={current.sourceFingerprint}
          runId={analysisRunId}
          activeSection={activeSection}
          unavailableMessage={compiled && current.aiReady !== false ? undefined : "AI 问答暂未就绪，不影响原文阅读和笔记。"}
          selectionRequest={selectionRequest}
          onSelectionAnswered={receiveSelectionAnswer}
        />
      </div>
    </div>
  </div>
}

const READER_FONT_SIZES = [16, 18, 20, 22] as const

function adjacentReaderFontSize(
  current: (typeof READER_FONT_SIZES)[number],
  direction: -1 | 1,
): (typeof READER_FONT_SIZES)[number] {
  const index = READER_FONT_SIZES.indexOf(current)
  const next = Math.max(0, Math.min(READER_FONT_SIZES.length - 1, index + direction))
  return READER_FONT_SIZES[next]
}
