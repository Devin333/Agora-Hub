"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { ArrowUpRight, BookOpen, Download, FileText, Focus, LoaderCircle, Minus, Plus, RefreshCw, Sparkles, X } from "lucide-react"
import { OpenReaderPage } from "@/components/papers/open-reader"
import type { ReaderSelectionAskAnswer, ReaderSelectionAskDraft, ReaderSelectionAskRequest } from "@/components/papers/open-reader/open-reader-types"
import { buildReaderParagraphs, buildReaderToc } from "@/components/papers/open-reader/open-reader-utils"
import { PaperPdfViewer } from "@/components/papers/shared/paper-pdf-viewer"
import { ReaderAnalysisControl } from "@/features/portal/reader/reader-analysis-control"
import { ReaderAssistant, safeReaderSourceUrl } from "@/features/portal/reader/reader-assistant"
import { ReaderPreparationControl } from "@/features/portal/reader/reader-preparation-control"
import { hasOwnedAnalysisSource, needsOwnedSourcePreparation, readerUnavailableMessage, type ReaderWorkspacePayload } from "@/features/portal/reader/reader-contract"
import { useReaderNotebook } from "@/features/portal/reader/use-reader-notebook"
import { isLocalDesktopProfile } from "@/lib/deployment-profile"
import { formatPaperDate } from "@/lib/papers/format"
import type { PaperReaderCitation } from "@/lib/papers/types"
import { downloadResearchReport, fetchDesktopReader, fetchResearchReport, saveResearchReport } from "@/lib/research/desktop/client"
import type { ResearchReport } from "@/lib/research/desktop/contracts"
import type { PersonalMemoryTaskPreview } from "@/lib/research/desktop/client"
import type { ReaderPersonalMemoryControl, ReaderTaskActivity } from "@/features/portal/reader/personal-memory-task-context"
import { normalizeAnalyzeTransportUrl } from "@/lib/research/desktop/request-contracts"
import { resolveCitationLocation } from "@/lib/research/reader-location"
import { matchesSelectionAnswerScope } from "@/lib/research/selection-ask"
import type { AppTheme, ReaderTab } from "./model"
import { activeReaderRequestIds, readerTaskSnapshots, usedReaderMemoryRefs } from "./reader-task-state"
import { saveUserExport } from "./user-export"
import styles from "./paper-agent-app.module.css"

type ReaderView = "text" | "pdf"

export function PaperReaderPane({
  paperId,
  online,
  assistantOpen,
  focusMode,
  initialTab,
  onAssistantClose,
  onFocusChange,
  onTabChange,
  useMemory,
  memoryAllowed,
  onUseMemoryChange,
  owner,
  onActivityChange,
  onTaskStateChange,
  appearance,
}: {
  paperId: string
  online: boolean
  assistantOpen: boolean
  focusMode: boolean
  initialTab: ReaderTab
  onAssistantClose: () => void
  onFocusChange: (value: boolean) => void
  onTabChange: (tab: ReaderTab) => void
  useMemory: boolean
  memoryAllowed: boolean
  onUseMemoryChange: (enabled: boolean) => void
  owner: string
  onActivityChange: (active: boolean) => void
  onTaskStateChange: (state: { activeRequestIds: string[]; unsaved: boolean }) => void
  appearance: AppTheme
}) {
  const [payload, setPayload] = useState<ReaderWorkspacePayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [attempt, setAttempt] = useState(0)

  useEffect(() => { onActivityChange(loading) }, [loading, onActivityChange])
  useEffect(() => () => onActivityChange(false), [onActivityChange])

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError("")
    setPayload(null)
    fetchDesktopReader(paperId, controller.signal)
      .then(value => setPayload({ ...value, aiReady: value.capabilities?.ask === true }))
      .catch(cause => {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "暂时无法读取论文。")
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [attempt, paperId])

  if (loading) return <ReaderState title="正在读取论文" detail="正在核对 owner、来源修订和真实阅读投影…" busy />
  if (!payload) return <ReaderState title="论文暂时不可阅读" detail={error || "尚未取得正文、私有 PDF 或明确摘要。"} onRetry={() => setAttempt(value => value + 1)} />

  return (
    <LoadedReader
      key={`${payload.paper.id}:${payload.sourceRevision ?? "metadata"}`}
      payload={payload}
      online={online}
      assistantOpen={assistantOpen}
      focusMode={focusMode}
      initialTab={initialTab}
      onAssistantClose={onAssistantClose}
      onFocusChange={onFocusChange}
      onTabChange={onTabChange}
      onRefresh={() => setAttempt(value => value + 1)}
      useMemory={useMemory}
      memoryAllowed={memoryAllowed}
      onUseMemoryChange={onUseMemoryChange}
      owner={owner}
      onTaskStateChange={onTaskStateChange}
      appearance={appearance}
    />
  )
}

function LoadedReader({
  payload: initialPayload,
  online,
  assistantOpen,
  focusMode,
  initialTab,
  onAssistantClose,
  onFocusChange,
  onTabChange,
  onRefresh,
  useMemory,
  memoryAllowed,
  onUseMemoryChange,
  owner,
  onTaskStateChange,
  appearance,
}: {
  payload: ReaderWorkspacePayload
  online: boolean
  assistantOpen: boolean
  focusMode: boolean
  initialTab: ReaderTab
  onAssistantClose: () => void
  onFocusChange: (value: boolean) => void
  onTabChange: (tab: ReaderTab) => void
  onRefresh: () => void
  useMemory: boolean
  memoryAllowed: boolean
  onUseMemoryChange: (enabled: boolean) => void
  owner: string
  onTaskStateChange: (state: { activeRequestIds: string[]; unsaved: boolean }) => void
  appearance: AppTheme
}) {
  const [payload, setPayload] = useState(initialPayload)
  const [tab, setTab] = useState<ReaderTab>(initialTab)
  const [status, setStatus] = useState("")
  const [refreshing, setRefreshing] = useState(false)
  const [sectionRequest, setSectionRequest] = useState<{ sourceIdentity: string; id: string; sequence: number }>()
  const [activeSectionState, setActiveSectionState] = useState<{ sourceIdentity: string; id: string }>()
  const [pdfLocatorSequence, setPdfLocatorSequence] = useState(0)
  const [pendingPdfLocator, setPendingPdfLocator] = useState<{ sourceIdentity: string; page: number }>()
  const pendingPdfLocatorRef = useRef<{ sourceIdentity: string; page: number } | undefined>(undefined)
  const sourceIdentityRef = useRef("")
  const restoredSourceIdentityRef = useRef("")
  const [selectionRequest, setSelectionRequest] = useState<ReaderSelectionAskRequest>()
  const [selectionAnswer, setSelectionAnswer] = useState<ReaderSelectionAskAnswer>()
  const sourceRevisionRef = useRef(payload.sourceRevision)
  const paper = payload.paper
  const sourceRevision = payload.sourceRevision ?? ""
  const notebook = useReaderNotebook(paper.id, sourceRevision)
  const updateNotebook = notebook.update
  const notebookPdfPageRef = useRef(notebook.pdfPage)
  notebookPdfPageRef.current = notebook.pdfPage
  const [memoryPreview, setMemoryPreview] = useState<PersonalMemoryTaskPreview | null>(null)
  const [taskActivities, setTaskActivities] = useState<Record<string, ReaderTaskActivity>>({})
  const personalMemory = useMemo<ReaderPersonalMemoryControl>(() => ({ enabled: useMemory && memoryAllowed, limit: 5, onPreview: setMemoryPreview }), [memoryAllowed, useMemory])
  const recordTaskActivity = useCallback((activity: ReaderTaskActivity) => {
    setTaskActivities(current => ({ ...current, [`${activity.kind}:${activity.requestId}`]: activity }))
  }, [])
  const activeRequestIds = useMemo(() => activeReaderRequestIds(Object.values(taskActivities)), [taskActivities])
  const usedMemoryRefs = useMemo(() => usedReaderMemoryRefs(Object.values(taskActivities)), [taskActivities])
  const taskSnapshots = useMemo(() => readerTaskSnapshots(Object.values(taskActivities)), [taskActivities])
  const outline = useMemo(() => payload.visualLayer?.outline?.length
    ? payload.visualLayer.outline
    : payload.reader ? buildReaderToc(buildReaderParagraphs(payload.reader, "zh")) : [], [payload.reader, payload.visualLayer])
  const sectionIds = useMemo(() => new Set(outline.map(item => item.id)), [outline])
  const sourceIdentityExact = Boolean(payload.sourceRevision && payload.sourceFingerprint)
  const sourceIdentity = `${paper.id}:${payload.sourceRevision ?? "metadata"}:${payload.sourceFingerprint ?? "unverified"}`
  const source = safeReaderSourceUrl(payload.sourceUrl || paper.paperUrl || paper.arxivUrl)
  const pdf = sourceIdentityExact ? safeReaderSourceUrl(paper.pdfUrl) : undefined
  const textAvailable = payload.state === "ready" && Boolean(payload.reader)
  const notebookMatchesSource = notebook.ready && notebook.sourceRevision === sourceRevision
  const storedReaderView: ReaderView = notebookMatchesSource && notebook.readerView === "pdf" ? "pdf" : "text"
  const readerView: ReaderView = storedReaderView === "pdf" && pdf ? "pdf" : textAvailable ? "text" : pdf ? "pdf" : "text"
  const activeSection = activeSectionState?.sourceIdentity === sourceIdentity ? activeSectionState.id : ""
  const activeSectionRequest = sectionRequest?.sourceIdentity === sourceIdentity
    ? { id: sectionRequest.id, sequence: sectionRequest.sequence }
    : undefined
  const requestedPdfPage = pendingPdfLocator?.sourceIdentity === sourceIdentity ? pendingPdfLocator.page : notebook.pdfPage
  const preparationSource = normalizeAnalyzeTransportUrl(source) ?? normalizeAnalyzeTransportUrl(pdf)
  const canAnalyze = hasOwnedAnalysisSource(payload)
  const needsPreparation = needsOwnedSourcePreparation(payload, isLocalDesktopProfile())
  sourceRevisionRef.current = payload.sourceRevision
  sourceIdentityRef.current = sourceIdentity
  pendingPdfLocatorRef.current = pendingPdfLocator

  useEffect(() => setTab(initialTab), [initialTab])
  useEffect(() => { if (!useMemory) setMemoryPreview(null) }, [useMemory])
  useEffect(() => { setSelectionRequest(undefined); setSelectionAnswer(undefined) }, [paper.id, payload.sourceRevision])
  useEffect(() => {
    if (pendingPdfLocatorRef.current?.sourceIdentity === sourceIdentity) return
    pendingPdfLocatorRef.current = undefined
    setPendingPdfLocator(undefined)
  }, [sourceIdentity])
  useEffect(() => {
    if (!notebookMatchesSource || restoredSourceIdentityRef.current === sourceIdentity) return
    restoredSourceIdentityRef.current = sourceIdentity
    if (notebook.revisionChanged) {
      setStatus("来源修订已更换，已安全重置阅读模式、章节和 PDF 页码。")
      return
    }
    if (!notebook.resumeSection) return
    if (!sectionIds.has(notebook.resumeSection)) {
      setStatus("保存的章节定位在当前来源修订中不可用，已停留在正文起点。")
      return
    }
    const id = notebook.resumeSection
    setActiveSectionState({ sourceIdentity, id })
    setSectionRequest(previous => ({
      sourceIdentity,
      id,
      sequence: (previous?.sequence ?? 0) + 1,
    }))
  }, [notebook.resumeSection, notebook.revisionChanged, notebookMatchesSource, sectionIds, sourceIdentity])
  useEffect(() => {
    onTaskStateChange({
      activeRequestIds,
      unsaved: Boolean(notebook.dirty) || notebook.storageError || notebook.noteOverLimit || notebook.saveStatus === "error" || notebook.saveStatus === "conflict",
    })
  }, [activeRequestIds, notebook.dirty, notebook.noteOverLimit, notebook.saveStatus, notebook.storageError, onTaskStateChange])
  useEffect(() => () => onTaskStateChange({ activeRequestIds: [], unsaved: false }), [onTaskStateChange])

  function selectTab(next: ReaderTab) {
    setTab(next)
    onTabChange(next)
  }

  const requestSelectionAsk = useCallback((draft: ReaderSelectionAskDraft) => {
    if (!owner || !payload.sourceRevision || !sourceIdentityExact) return
    setSelectionAnswer(undefined)
    setSelectionRequest({ ...draft, requestKey: crypto.randomUUID(), owner, sourceRevision: payload.sourceRevision })
  }, [owner, payload.sourceRevision, sourceIdentityExact])

  const receiveSelectionAnswer = useCallback((answer: ReaderSelectionAskAnswer) => {
    if (!matchesSelectionAnswerScope(answer, owner, sourceRevisionRef.current, selectionRequest?.requestKey)) return
    setSelectionAnswer(answer)
  }, [owner, selectionRequest?.requestKey])

  const handlePdfPageChange = useCallback((page: number, numPages: number) => {
    if (sourceIdentityRef.current !== sourceIdentity) return
    const pending = pendingPdfLocatorRef.current
    if (pending?.sourceIdentity === sourceIdentity) {
      pendingPdfLocatorRef.current = undefined
      setPendingPdfLocator(undefined)
      if (pending.page > numPages || page !== pending.page) {
        setStatus(`引用页 ${pending.page} 超出当前 PDF 的 ${numPages} 页范围，定位已失效。`)
      } else {
        setStatus(`已定位到 PDF 第 ${page} 页。`)
      }
    }
    if (notebookPdfPageRef.current !== page) {
      notebookPdfPageRef.current = page
      updateNotebook({ pdfPage: page })
    }
  }, [sourceIdentity, updateNotebook])

  const handlePdfLoadError = useCallback(() => {
    if (sourceIdentityRef.current !== sourceIdentity) return
    const pending = pendingPdfLocatorRef.current
    if (pending?.sourceIdentity !== sourceIdentity) return
    pendingPdfLocatorRef.current = undefined
    setPendingPdfLocator(undefined)
    setStatus(`PDF 加载失败，未能定位到第 ${pending.page} 页。`)
  }, [sourceIdentity])

  function selectReaderView(next: ReaderView) {
    if (!notebookMatchesSource) return
    if ((next === "pdf" && !pdf) || (next === "text" && !textAvailable)) return
    if (next === "text" && pendingPdfLocatorRef.current?.sourceIdentity === sourceIdentity) {
      pendingPdfLocatorRef.current = undefined
      setPendingPdfLocator(undefined)
      setStatus("")
    }
    updateNotebook({ readerView: next })
  }

  function navigateCitation(citation: PaperReaderCitation) {
    const resolution = resolveCitationLocation(citation, payload.sourceRevision, sectionIds, Boolean(pdf))
    if (!resolution.allowed) {
      setStatus(resolution.reason === "source_revision_mismatch" ? "引用来自另一来源修订，未执行定位。" : "该引用没有当前修订可核对的精确位置。")
      return
    }
    if (!notebookMatchesSource) {
      setStatus("阅读位置仍在恢复，请稍后再次打开该引用。")
      return
    }
    if (resolution.location.page !== undefined) {
      const pending = { sourceIdentity, page: resolution.location.page }
      pendingPdfLocatorRef.current = pending
      setPendingPdfLocator(pending)
      updateNotebook({ readerView: "pdf" })
      setPdfLocatorSequence(value => value + 1)
      selectTab("original")
      setStatus(`正在打开 PDF 第 ${resolution.location.page} 页…`)
      return
    }
    if (resolution.location.sectionId) {
      const id = resolution.location.sectionId
      pendingPdfLocatorRef.current = undefined
      setPendingPdfLocator(undefined)
      updateNotebook({ sectionId: id, readerView: "text" })
      setActiveSectionState({ sourceIdentity, id })
      setSectionRequest(previous => ({ sourceIdentity, id, sequence: (previous?.sequence ?? 0) + 1 }))
      selectTab("original")
      setStatus("已定位到当前修订的正文章节。")
    }
  }

  async function refresh(expectedRunId?: string) {
    setRefreshing(true)
    setStatus("")
    try {
      const next = await fetchDesktopReader(paper.id)
      if (expectedRunId && next.runId !== expectedRunId) throw new Error("新分析运行尚未成为当前可核对的阅读投影。")
      setPayload({ ...next, aiReady: next.capabilities?.ask === true })
    } catch (cause) {
      setStatus(cause instanceof Error ? cause.message : "刷新失败。")
    } finally {
      setRefreshing(false)
    }
  }

  async function exportNote() {
    const title = paper.title.replace(/[\r\n]+/g, " ").trim()
    const content = `# ${title}\n\n- Paper ID: ${paper.id}\n- Source revision: ${sourceRevision || "unconfirmed"}\n\n## Notes\n\n${notebook.note || ""}\n`
    const result = await saveUserExport({ suggestedName: `${title || paper.id}-notes`, format: "md", content })
    setStatus(result.ok ? result.message : result.code === "cancelled" ? "已取消笔记导出。" : result.message)
  }
  const noteStatus = !notebook.ready ? "正在读取笔记…"
    : notebook.noteOverLimit ? "笔记超过 20,000 字，未覆盖已保存内容。"
      : notebook.storageError ? "保存失败，草稿仍保留在当前页面。"
        : notebook.saveStatus === "conflict" ? "另一位置已更新工作区；当前草稿仍保留。"
          : notebook.saveStatus === "saving" ? "正在保存到研究工作区…"
            : notebook.saveStatus === "synced" ? "已保存到你的研究工作区" : notebook.saveStatus === "error" ? "服务端保存失败，草稿仍保留在当前页面。" : "笔记已载入；尚无新的服务端保存回执"

  return (
    <section className={`${styles.readerPane} ${assistantOpen && !focusMode ? styles.readerWithAssistant : ""}`} aria-label="论文阅读区">
      <header className={styles.readerHeader}>
        <div className={styles.readerTitleBlock}>
          <span>{paper.venue || "RESEARCH PAPER"} · {formatPaperDate(paper.publishedAt, "zh")}</span>
          <h1>{paper.title}</h1>
          <p>{paper.authors.join(", ") || "作者信息未提供"}</p>
        </div>
        <div className={styles.readerHeaderActions}>
          {source && <a href={source} target="_blank" rel="noreferrer">来源<ArrowUpRight size={14} /></a>}
          <button type="button" aria-pressed={focusMode} onClick={() => onFocusChange(!focusMode)}><Focus size={15} />{focusMode ? "退出专注" : "专注"}</button>
        </div>
      </header>

      <div className={styles.readerToolbar}>
        <div role="tablist" aria-label="论文内容">
          {(["original", "overview", "notes", "details"] as const).map(item => (
            <button type="button" role="tab" aria-selected={tab === item} key={item} onClick={() => selectTab(item)}>
              {{ original: "原文", overview: "概览", notes: "笔记", details: "详情" }[item]}
            </button>
          ))}
        </div>
        <div className={styles.readerToolbarActions}>
          {tab === "original" && (textAvailable || pdf) && <div className={styles.readerModeSwitch} role="group" aria-label="原文阅读模式">
            <button type="button" aria-pressed={readerView === "text"} disabled={!notebookMatchesSource || !textAvailable} onClick={() => selectReaderView("text")}>文本</button>
            {pdf && <button type="button" aria-pressed={readerView === "pdf"} disabled={!notebookMatchesSource} onClick={() => selectReaderView("pdf")}>PDF</button>}
          </div>}
          <div className={styles.fontControls}>
            <button type="button" aria-label="缩小正文字号" disabled={!notebook.ready || notebook.fontSize <= 16} onClick={() => notebook.update({ fontSize: Math.max(16, notebook.fontSize - 2) as 16 | 18 | 20 | 22 })}><Minus size={14} /></button>
            <span>{notebook.fontSize}px</span>
            <button type="button" aria-label="放大正文字号" disabled={!notebook.ready || notebook.fontSize >= 22} onClick={() => notebook.update({ fontSize: Math.min(22, notebook.fontSize + 2) as 16 | 18 | 20 | 22 })}><Plus size={14} /></button>
          </div>
        </div>
      </div>

      {!online && <p role="status" className={styles.readerStatus}>当前离线：本地原文、历史成果和笔记仍可使用；新的正文准备、分析与问答不会排队或自动补发。</p>}
      {(status || refreshing) && <p role="status" className={styles.readerStatus}>{refreshing ? "正在刷新真实阅读投影…" : status}</p>}
      <div className={styles.readerScroll}>
        {tab === "original" && (
          !notebook.ready ? (
            <ReaderState title="正在恢复阅读位置" detail="正在核对 owner、来源修订和已保存的阅读视图…" busy />
          ) : readerView === "pdf" && pdf ? (
            <PaperPdfViewer
              key={`${sourceIdentity}:${pdfLocatorSequence}`}
              pdfUrl={pdf}
              title={paper.title}
              locale="zh"
              appearance="paper-agent"
              paperId={paper.id}
              initialPage={requestedPdfPage}
              onPageChange={handlePdfPageChange}
              onLoadError={handlePdfLoadError}
              className={styles.pdfViewer}
              errorMessage="PDF 暂时加载失败；可打开来源或重试。"
              fallback={<DocumentUnavailable message="PDF 暂时加载失败。" source={source} onRetry={onRefresh} />}
            />
          ) : payload.state === "ready" && payload.reader ? (
            <OpenReaderPage
              key={sourceIdentity}
              reader={payload.reader}
              locale="zh"
              visualLayer={payload.visualLayer}
              workspace={{
                fontSize: notebook.fontSize,
                appearance: appearance === "dark" ? "monochrome-dark" : "monochrome-light",
                sourceRevision: payload.sourceRevision,
                sectionRequest: activeSectionRequest,
                onSectionChange: id => { setActiveSectionState({ sourceIdentity, id }); notebook.update({ sectionId: id }) },
                onSelectionAsk: requestSelectionAsk,
                selectionAnswer,
                onSelectionAnswerApplied: requestKey => {
                  if (selectionAnswer?.requestKey === requestKey) { setSelectionAnswer(undefined); setSelectionRequest(undefined) }
                },
              }}
            />
          ) : <DocumentUnavailable message={readerUnavailableMessage(payload.reasonCode)} source={source} onRetry={() => void refresh()} />
        )}

        {tab === "overview" && <article className={styles.proseCard}>
          <span className={styles.eyebrow}>ABSTRACT / 明确证据范围</span>
          <h2>论文概览</h2>
          <p>{paper.abstractSnippet || "当前没有可核对的摘要；请打开论文来源或导入 PDF。"}</p>
          <div className={styles.runtimeControls}>
            {needsPreparation && <ReaderPreparationControl paper={paper} sourceUrl={preparationSource ?? undefined} enabled={online && Boolean(preparationSource)} onReady={() => refresh()} />}
            <ReaderAnalysisControl paper={paper} sourceRevision={sourceRevision} sourceFingerprint={payload.sourceFingerprint} sourceUrl={source} pdfUrl={pdf} initialRunId={payload.runId} enabled={online && canAnalyze} onRun={runId => refresh(runId)} personalMemory={personalMemory} onTaskActivity={recordTaskActivity} />
          </div>
        </article>}

        {tab === "notes" && <section className={styles.noteCard}>
          <label htmlFor={`paper-note-${paper.id}`}>我的阅读笔记</label>
          <textarea id={`paper-note-${paper.id}`} value={notebook.note} disabled={!notebook.ready || notebook.resolving} maxLength={20_000} onChange={event => notebook.update({ note: event.target.value })} placeholder="记录理解、疑问和下一步验证。" />
          <div className={styles.noteActions}><p role="status" data-state={notebook.storageError || notebook.saveStatus === "conflict" ? "error" : "ok"}>{noteStatus}</p><button type="button" disabled={!notebook.ready} onClick={() => void exportNote()}><Download size={14} />导出 Markdown</button></div>
          {notebook.saveError && <p role="alert">{notebook.saveError}</p>}
          {notebook.saveStatus === "conflict" ? <div className={styles.noteActions}><button type="button" disabled={notebook.resolving} onClick={() => void notebook.resolveConflict("keep-draft")}>保留本页草稿并核对最新 revision</button><button type="button" disabled={notebook.resolving} onClick={() => { if (window.confirm("采用服务端最新笔记将放弃本页未保存草稿。是否继续？")) void notebook.resolveConflict("use-server") }}>采用服务端版本</button></div> : notebook.dirty || notebook.saveStatus === "error" ? <div className={styles.noteActions}><button type="button" disabled={notebook.resolving || notebook.noteOverLimit} onClick={() => notebook.retrySave()}>重试保存草稿</button></div> : null}
        </section>}

        {tab === "details" && <section className={styles.detailsGrid}>
          <div><span>Paper ID</span><code>{paper.id}</code></div>
          <div><span>Source revision</span><code>{payload.sourceRevision || "仅元数据 / 未确认"}</code></div>
          <div><span>Source fingerprint</span><code>{payload.sourceFingerprint || "未确认"}</code></div>
          <div><span>Evidence scope</span><strong>{readerEvidenceScope(payload, Boolean(pdf))}</strong></div>{payload.reader?.quality ? <><div><span>Text extraction</span><strong>{payload.reader.quality.textExtracted ? "已提取；不代表全页完整" : "未确认"}</strong></div><div><span>Evidence coverage</span><strong>{Math.round(payload.reader.quality.evidenceCoverage * 100)}%（证据索引指标，非全文覆盖率）</strong></div></> : null}
          <ReportControls key={`${owner}:${paper.id}:${sourceRevision}:${payload.runId ?? "none"}`} reportRunId={payload.runId} paperId={paper.id} sourceRevision={sourceRevision} title={paper.title} />
        </section>}
      </div>

      {assistantOpen && !focusMode && <aside className={styles.assistantPanel} role="complementary" aria-label="论文助手">
        <div className={styles.assistantTopbar}>
          <div><Sparkles size={16} /><strong>论文助手</strong></div>
          <button type="button" className={styles.iconButton} aria-label="关闭论文助手" onClick={onAssistantClose}><X size={17} /></button>
        </div>
        <div className={styles.memoryDisclosure} data-enabled={useMemory || undefined}>
          <label className={styles.assistantMemoryToggle} title={memoryAllowed ? "每次任务默认关闭；选中后才使用已授权记忆。" : "请先在个人记忆中保存允许远程处理的 policy。"}>
            <input type="checkbox" checked={useMemory} disabled={!memoryAllowed} onChange={event => onUseMemoryChange(event.target.checked)} />
            本次助手任务使用已授权记忆
          </label>
          <strong>{useMemory ? "本任务已启用受控个人记忆" : "本次不使用个人记忆"}</strong>
          {taskSnapshots.length > 0 && <div className={styles.taskIdentities}><small>真实任务身份与服务端状态</small><ul>{taskSnapshots.map(task => <li key={`${task.kind}:${task.requestId}`}><span>{task.kind === "analyze" ? "分析" : "问答"} · {readerTaskStatusLabel(task.status)}</span><code>{task.requestId}</code><small>{task.generation}</small></li>)}</ul></div>}
          {useMemory && memoryPreview?.selection ? <><small>授权快照 · policy r{memoryPreview.selection.policyRevision}</small><ul>{memoryPreview.selection.refs.map(ref => <li key={ref.memoryId}><code>{ref.memoryId}@r{ref.revision}</code></li>)}</ul></> : null}
          {useMemory && memoryPreview && !memoryPreview.selection ? <small>{memoryPreview.reason === "policy_disabled" ? "远程处理 policy 未授权。" : memoryPreview.reason === "no_match" ? "本次 recall 没有匹配记忆。" : "等待任务 recall preview。"}</small> : null}
          {usedMemoryRefs.length > 0 ? <><small>服务端实际 used refs</small><ul>{usedMemoryRefs.map(ref => <li key={`${ref.memoryId}:${ref.revision}`}><code>{ref.memoryId}@r{ref.revision}</code></li>)}</ul></> : null}
        </div>
        <ReaderAssistant
          paper={paper}
          notebook={notebook}
          sectionIds={sectionIds}
          onNavigateCitation={navigateCitation}
          researchQuestion=""
          sourceRevision={sourceRevision}
          sourceFingerprint={payload.sourceFingerprint}
          runId={payload.runId}
          activeSection={activeSection}
          unavailableMessage={!online ? "当前离线，新的远程问答不会排队或自动补发。" : payload.reader && payload.aiReady !== false ? undefined : "AI 问答尚未就绪，不影响原文和笔记。"}
          selectionRequest={selectionRequest}
          onSelectionAnswered={receiveSelectionAnswer}
          personalMemory={personalMemory}
          onTaskActivity={recordTaskActivity}
          onExportNote={async value => {
            const result = await saveUserExport({ suggestedName: value.filename.replace(/\.md$/i, ""), format: "md", content: value.content })
            setStatus(result.ok ? result.message : result.code === "cancelled" ? "已取消笔记导出。" : result.message)
          }}
        />
      </aside>}
    </section>
  )
}

function DocumentUnavailable({ message, source, onRetry }: { message: string; source?: string; onRetry: () => void }) {
  return <div className={styles.documentUnavailable}><BookOpen size={28} /><h2>原文尚未就绪</h2><p>{message}</p><div>{source && <a href={source} target="_blank" rel="noreferrer">打开论文来源<ArrowUpRight size={14} /></a>}<button type="button" onClick={onRetry}><RefreshCw size={14} />重新获取</button></div></div>
}

function ReaderState({ title, detail, busy, onRetry }: { title: string; detail: string; busy?: boolean; onRetry?: () => void }) {
  return <section className={styles.readerState} role={busy ? "status" : "alert"}>{busy ? <LoaderCircle className={styles.spinner} size={24} /> : <FileText size={24} />}<h1>{title}</h1><p>{detail}</p>{onRetry && <button type="button" className={styles.secondaryButton} onClick={onRetry}>重试</button>}</section>
}

function ReportControls({ reportRunId, paperId, sourceRevision, title }: { reportRunId?: string; paperId: string; sourceRevision: string; title: string }) {
  const [report, setReport] = useState<ResearchReport | null>(null)
  const [status, setStatus] = useState("")
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    setReport(null)
    setStatus("")
  }, [paperId, reportRunId, sourceRevision])

  const acceptedExport = report !== null && report.runId === reportRunId && report.paperId === paperId && report.sourceRevision === sourceRevision && report.eligibility.accepted === true && report.eligibility.qualityPassed === true && report.eligibility.acceptedExport === true

  async function load() {
    if (!reportRunId || !sourceRevision) return
    setBusy(true); setStatus("")
    try {
      const next = await fetchResearchReport(reportRunId, sourceRevision)
      if (next.runId !== reportRunId || next.paperId !== paperId || next.sourceRevision !== sourceRevision) throw new Error("报告回执与当前 paper/run/source revision 不一致，未采用该结果。")
      setReport(next)
      setStatus(reportBoundaryLabel(next))
    } catch (cause) {
      setReport(null)
      setStatus(cause instanceof Error ? cause.message : "报告读取失败。")
    } finally { setBusy(false) }
  }

  async function save() {
    if (!report || !acceptedExport) return
    setBusy(true); setStatus("")
    try {
      const saved = await saveResearchReport({ reportId: `report-${report.runId}`, runId: report.runId, sourceRevision: report.sourceRevision, title })
      if (saved.reference.runId !== report.runId || saved.reference.paperId !== paperId || saved.reference.sourceRevision !== report.sourceRevision) throw new Error("报告保存回执与当前 paper/run/source revision 不一致。")
      setStatus(`合格报告已保存：${saved.reference.reportId}`)
    } catch (cause) { setStatus(cause instanceof Error ? cause.message : "报告保存失败。") }
    finally { setBusy(false) }
  }

  async function download(format: "markdown" | "json") {
    if (!report || !acceptedExport) return
    setBusy(true); setStatus("")
    try {
      const value = await downloadResearchReport(report.runId, report.sourceRevision, format)
      const exportFormat = format === "markdown" ? "md" : "json"
      const result = await saveUserExport({ suggestedName: value.filename.replace(/\.(?:md|json)$/i, ""), format: exportFormat, content: await value.blob.text() })
      setStatus(result.ok ? result.message : result.code === "cancelled" ? "已取消报告导出。" : result.message)
    } catch (cause) { setStatus(cause instanceof Error ? cause.message : "报告导出失败。") }
    finally { setBusy(false) }
  }

  return <div className={styles.reportControls}>
    <span>分析报告</span>
    {!reportRunId ? <p>尚无可核对的分析 run；不会提前显示质量通过。</p> : !sourceRevision ? <p>当前来源修订未确认，不能读取或导出报告。</p> : <div>
      <button type="button" disabled={busy} onClick={() => void load()}>{busy ? <LoaderCircle className={styles.spinner} size={14} /> : <RefreshCw size={14} />}{report ? "刷新报告边界" : "读取真实报告"}</button>
      <button type="button" disabled={!acceptedExport || busy} onClick={() => void save()}>保存合格报告</button>
      <button type="button" disabled={!acceptedExport || busy} onClick={() => void download("markdown")}><Download size={14} />Markdown</button>
      <button type="button" disabled={!acceptedExport || busy} onClick={() => void download("json")}><Download size={14} />结构化 JSON</button>
    </div>}
    {report && <dl className={styles.reportBoundary}><div><dt>Run</dt><dd><code>{report.runId}</code></dd></div><div><dt>Source revision</dt><dd><code>{report.sourceRevision}</code></dd></div><div><dt>Candidate status</dt><dd>{report.status}</dd></div><div><dt>Deterministic gate</dt><dd>{report.eligibility.qualityPassed ? "质量门通过" : "质量门未通过"}</dd></div><div><dt>Save / export boundary</dt><dd>{acceptedExport ? "已接受，可保存并原生导出" : report.eligibility.accepted ? "已接受，但不可作为合格导出" : "候选/草稿，不可保存为合格报告"}</dd></div><div><dt>Artifact</dt><dd>{report.diagnostics.artifactStatus}{report.diagnostics.artifactReason ? ` · ${report.diagnostics.artifactReason}` : ""}</dd></div></dl>}
    {status && <p role="status">{status}</p>}
  </div>
}

function reportBoundaryLabel(report: ResearchReport): string {
  if (report.eligibility.accepted && report.eligibility.qualityPassed && report.eligibility.acceptedExport) return "报告已通过 deterministic gate，并具备 accepted export 资格。"
  if (report.eligibility.accepted) return "报告候选已接受，但当前 artifact 尚不具备合格导出资格。"
  if (report.eligibility.qualityPassed) return "质量投影通过，但报告尚未被接受；仍按候选处理。"
  return "报告仍是草稿/被拒候选，不会显示为质量通过，也不能导出为合格结果。"
}

function readerEvidenceScope(payload: ReaderWorkspacePayload, hasPdf: boolean): string {
  if (payload.state === "ready" && payload.reader?.quality.textExtracted) return "结构化文本已提取；当前投影未声明全文完整"
  if (payload.reader) return "部分解析 / 可核对原文片段"
  if (hasPdf) return "原件 PDF 可读；解析范围未确认"
  if (payload.paper.abstractSnippet) return "仅摘要"
  return "未就绪 / 仅元数据"
}

function readerTaskStatusLabel(status: ReaderTaskActivity["status"]): string {
  if (status === "passed") return "服务端已通过"
  if (status === "rejected") return "质量门拒绝"
  if (status === "failed") return "运行失败"
  if (status === "needs_action") return "需要核对"
  if (status === "interrupted" || status === "unknown") return "结果待核对"
  if (status === "running") return "运行中"
  return "等待中"
}

