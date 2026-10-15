"use client"

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react"
import { ArrowUp, BookOpen, Download, FilePenLine, Quote, Sparkles, Square } from "lucide-react"
import ReactMarkdown from "react-markdown"
import type {
  ReaderSelectionAskAnswer,
  ReaderSelectionAskRequest,
} from "@/components/papers/open-reader/open-reader-types"
import { isLocalDesktopProfile } from "@/lib/deployment-profile"
import { askPaper } from "@/lib/papers/api"
import type { Paper, PaperReaderAnswer, PaperReaderCitation } from "@/lib/papers/types"
import {
  fetchDesktopIdentity,
  fetchResearchOperation,
  submitResearchAsk,
} from "@/lib/research/desktop/client"
import { groundedAnswerSchema, type ResearchOperation } from "@/lib/research/desktop/contracts"
import type {
  ResearchPendingOperation,
  ResearchPersonalMemorySelection,
} from "@/lib/research/workspace-items"
import { buildAskRequest } from "@/lib/research/desktop/request-contracts"
import {
  operationReceiptMatches,
  prepareResearchOperation,
  recoverableResearchOperations,
  updateResearchOperation,
  type OperationReceiptIdentity,
} from "@/lib/research/pending-operations"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import {
  resolveReaderPersonalMemory,
  usedPersonalMemoryFromResult,
  usedPersonalMemoryMatches,
  type ReaderPersonalMemoryControl,
  type ReaderTaskActivity,
} from "./personal-memory-task-context"
import type { useReaderNotebook } from "./use-reader-notebook"
import styles from "./reader-workspace.module.css"

export function safeReaderSourceUrl(value?: string): string | undefined {
  if (!value) return undefined
  try {
    if (value.startsWith("/")) {
      const url = new URL(value, "https://newsroom.invalid")
      const allowedPath = /^\/api\/v1\/research\/desktop\/papers\/[A-Za-z0-9._:%-]+\/source$/
      const revisions = url.searchParams.getAll("sourceRevision")
      const fingerprints = url.searchParams.getAll("sourceFingerprint")
      const keys = [...url.searchParams.keys()]
      const encodedPaperId = url.pathname.split("/").at(-2) ?? ""
      let paperId = ""
      try {
        paperId = decodeURIComponent(encodedPaperId)
      } catch {
        return undefined
      }
      if (
        url.origin !== "https://newsroom.invalid" ||
        url.hash ||
        !allowedPath.test(url.pathname) ||
        !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(paperId) ||
        keys.some(key => key !== "sourceRevision" && key !== "sourceFingerprint") ||
        revisions.length !== 1 ||
        fingerprints.length !== 1 ||
        !revisions[0] ||
        revisions[0].length > 512 ||
        /[\r\n]/.test(revisions[0]) ||
        !/^sha256:[0-9a-f]{64}$/.test(fingerprints[0])
      ) {
        return undefined
      }
      return `${url.pathname}${url.search}`
    }

    const url = new URL(value)
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
      return undefined
    }
    return url.href
  } catch {
    return undefined
  }
}

type Notebook = ReturnType<typeof useReaderNotebook>

export type ReaderAssistantProps = {
  paper: Paper
  notebook: Notebook
  sectionIds: Set<string>
  onNavigateCitation: (citation: PaperReaderCitation) => void
  researchQuestion: string
  sourceRevision: string
  sourceFingerprint?: string
  runId?: string
  activeSection?: string
  unavailableMessage?: string
  selectionRequest?: ReaderSelectionAskRequest
  onSelectionAnswered?: (answer: ReaderSelectionAskAnswer) => void
  personalMemory?: ReaderPersonalMemoryControl
  onTaskActivity?: (activity: ReaderTaskActivity) => void
  onUnsavedChanges?: (hasUnsavedChanges: boolean) => void
  onExportNote?: (value: { filename: string; content: string }) => void | Promise<void>
}

const prompts = [
  "这篇论文解决了什么问题？",
  "核心方法与已有工作有什么不同？",
  "实验支持了哪些结论，有什么局限？",
]

export function ReaderAssistant({
  paper,
  notebook,
  sectionIds,
  onNavigateCitation,
  researchQuestion,
  sourceRevision,
  sourceFingerprint,
  runId,
  activeSection,
  unavailableMessage,
  selectionRequest,
  onSelectionAnswered,
  personalMemory,
  onTaskActivity,
  onUnsavedChanges,
  onExportNote,
}: ReaderAssistantProps) {
  const desktopProfile = isLocalDesktopProfile()
  const workspace = useOwnedResearchWorkspace()
  const owner = workspace.owner
  const ownerRef = useRef(owner)
  const sourceRevisionRef = useRef(sourceRevision)
  const runIdRef = useRef(runId)
  const selectionRequestRef = useRef(selectionRequest)
  const paperIdRef = useRef(paper.id)
  const notebookQuestionRef = useRef(notebook.question)
  const selectionAnsweredRef = useRef(onSelectionAnswered)
  const personalMemoryRef = useRef(personalMemory)
  const taskActivityRef = useRef(onTaskActivity)
  const operationPersonalMemoryRef = useRef<ResearchPersonalMemorySelection | undefined>(undefined)
  ownerRef.current = owner
  sourceRevisionRef.current = sourceRevision
  runIdRef.current = runId
  selectionRequestRef.current = selectionRequest
  paperIdRef.current = paper.id
  notebookQuestionRef.current = notebook.question
  selectionAnsweredRef.current = onSelectionAnswered
  personalMemoryRef.current = personalMemory
  taskActivityRef.current = onTaskActivity
  const [tab, setTab] = useState<"ask" | "notes">("ask")
  const [answer, setAnswer] = useState<PaperReaderAnswer | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  const [requestId, setRequestId] = useState("")
  const [requestGeneration, setRequestGeneration] = useState("")
  const [operationStatus, setOperationStatus] = useState<ResearchOperation["status"] | "">("")
  const operationIdentityRef = useRef<OperationReceiptIdentity | null>(null)
  const request = useRef<AbortController | null>(null)
  const questionRef = useRef<HTMLTextAreaElement>(null)
  const replyRef = useRef<HTMLDivElement>(null)
  const updateNotebookRef = useRef(notebook.update)
  useLayoutEffect(() => {
    updateNotebookRef.current = notebook.update
  }, [notebook.update])
  useEffect(() => {
    onUnsavedChanges?.(notebook.saveStatus !== "synced")
  }, [notebook.saveStatus, onUnsavedChanges])

  const applyOperation = useCallback(async (
    operation: ResearchOperation,
    expectedOwner: string | null | undefined,
    expectedRevision: string,
    expectedRunId: string | undefined,
    expectedOperation: OperationReceiptIdentity,
    expectedSelection?: ReaderSelectionAskRequest,
    expectedPersonalMemory?: ResearchPersonalMemorySelection,
  ) => {
    if (
      ownerRef.current !== expectedOwner ||
      sourceRevisionRef.current !== expectedRevision ||
      runIdRef.current !== expectedRunId ||
      (expectedSelection != null &&
        selectionRequestRef.current != null &&
        selectionRequestRef.current.requestKey !== expectedSelection.requestKey)
    ) return
    if (!operationReceiptMatches(operation, expectedOperation)) {
      updateResearchOperation(expectedOperation.requestId, { status: "unknown" })
      setOperationStatus("unknown")
      setRequestGeneration(expectedOperation.generation)
      setRequestId(expectedOperation.requestId)
      setError("问答回执与已保存的请求身份不一致，请检查原请求，不能采用该回答。")
      return
    }
    if (
      operation.status === "passed" &&
      !usedPersonalMemoryMatches(expectedPersonalMemory, operation.result)
    ) {
      updateResearchOperation(expectedOperation.requestId, { status: "unknown" })
      setOperationStatus("unknown")
      setError("问答返回的个人记忆披露与原请求快照不一致，不能采用该回答。")
      taskActivityRef.current?.({
        kind: "ask",
        requestId: operation.requestId,
        generation: operation.generation,
        status: "unknown",
        usedPersonalMemory: [],
      })
      return
    }
    setOperationStatus(operation.status)
    setRequestGeneration(operation.generation)
    setRequestId(operation.requestId)
    updateResearchOperation(operation.requestId, {
      status: operation.status,
      ...(operation.runId ? { runId: operation.runId } : {}),
      ...(operation.errorCode ? { errorCode: operation.errorCode } : {}),
    })
    taskActivityRef.current?.({
      kind: "ask",
      requestId: operation.requestId,
      generation: operation.generation,
      status: operation.status,
      usedPersonalMemory: usedPersonalMemoryFromResult(operation.result),
    })

    if (operation.status === "passed") {
      const parsed = answerFromResult(
        operation.result,
        paperIdRef.current,
        notebookQuestionRef.current,
        expectedRunId,
        expectedRevision,
      )
      if (!parsed) {
        setAnswer(null)
        setError("问答已完成，但返回内容无法安全呈现。请从运行记录核对结果。")
        return
      }
      setAnswer(parsed)
      setError("")
      if (
        expectedSelection &&
        expectedSelection.owner === expectedOwner &&
        expectedSelection.sourceRevision === expectedRevision
      ) {
        selectionAnsweredRef.current?.({
          requestKey: expectedSelection.requestKey,
          selectionId: expectedSelection.selectionId,
          mode: expectedSelection.mode,
          question: expectedSelection.question,
          answer: parsed.answer,
          selection: expectedSelection.selection,
          owner: expectedSelection.owner,
          sourceRevision: expectedSelection.sourceRevision,
        })
      }
      window.requestAnimationFrame(() => replyRef.current?.focus())
      return
    }

    if (["failed", "rejected", "needs_action"].includes(operation.status)) {
      setError(
        operation.message ||
          (operation.status === "needs_action"
            ? "本次问答需要人工核对，不能作为已接受回答。"
            : "问答运行失败，请查看诊断后创建新的请求。"),
      )
      return
    }
    setError("任务已接收，正在等待真实运行状态。稍后可按请求身份继续核对。")
  }, [])


  useEffect(() => () => request.current?.abort(), [])

  useEffect(() => {
    request.current?.abort()
    setPending(false)
    setAnswer(null)
    setError("")
    operationIdentityRef.current = null
    operationPersonalMemoryRef.current = undefined
    setRequestId("")
    setRequestGeneration("")
    setOperationStatus("")

    if (!desktopProfile || !owner || !sourceRevision) return
    const existing = recoverableResearchOperations("ask").find(item =>
      item.paperId === paper.id &&
      item.sourceRevision === sourceRevision &&
      item.semantic.kind === "ask" &&
      item.semantic.runId === runId &&
      item.semantic.sourceFingerprint === sourceFingerprint,
    )
    if (!existing) return

    const controller = new AbortController()
    const expectedOwner = owner
    const recoveredSelection = recoverSelectionRequest(existing, expectedOwner)
    operationIdentityRef.current = existing
    operationPersonalMemoryRef.current = existing.semantic.kind === "ask"
      ? existing.semantic.personalMemory
      : undefined
    setRequestId(existing.requestId)
    setRequestGeneration(existing.generation)
    setOperationStatus(existing.status === "prepared" ? "waiting" : existing.status)
    void fetchResearchOperation(existing.requestId, existing.generation, controller.signal)
      .then(operation => {
        if (!controller.signal.aborted) {
          return applyOperation(
            operation,
            expectedOwner,
            sourceRevision,
            runId,
            existing,
            recoveredSelection,
            operationPersonalMemoryRef.current,
          )
        }
      })
      .catch(() => {
        if (
          ownerRef.current === expectedOwner &&
          sourceRevisionRef.current === sourceRevision &&
          runIdRef.current === runId &&
          !controller.signal.aborted
        ) {
          setError("上次问答结果仍待核对，不会自动重复提交。")
        }
      })
    return () => controller.abort()
  }, [applyOperation, desktopProfile, owner, paper.id, runId, selectionRequest, sourceFingerprint, sourceRevision])

  useEffect(() => {
    if (
      !selectionRequest ||
      selectionRequest.owner !== owner ||
      selectionRequest.sourceRevision !== sourceRevision
    ) {
      request.current?.abort()
      return
    }
    request.current?.abort()
    setPending(false)
    setTab("ask")
    setAnswer(null)
    setError("")
    setRequestId("")
    setRequestGeneration("")
    setOperationStatus("")
    operationPersonalMemoryRef.current = undefined
    updateNotebookRef.current({ question: selectionRequest.question })
    window.requestAnimationFrame(() => questionRef.current?.focus())
  }, [owner, selectionRequest, sourceRevision])

  async function submit() {
    const question = notebook.question.trim()
    if (!question || pending || unavailableMessage) return
    if (desktopProfile && (!owner || !runId || !sourceFingerprint || !sourceRevision)) {
      setError("这篇论文尚未绑定可核对的已接受分析运行，暂时不能问答。")
      return
    }

    const expectedOwner = owner
    const expectedRevision = sourceRevision
    const expectedRunId = runId
    const expectedSelection = selectionRequest
    const expectedMemoryEnabled = personalMemory?.enabled === true
    const controller = new AbortController()
    const identityChanged = () =>
      ownerRef.current !== expectedOwner ||
      sourceRevisionRef.current !== expectedRevision ||
      runIdRef.current !== expectedRunId ||
      (expectedSelection != null &&
        selectionRequestRef.current?.requestKey !== expectedSelection.requestKey) ||
      (personalMemoryRef.current?.enabled === true) !== expectedMemoryEnabled
    const scopeChanged = () => controller.signal.aborted || identityChanged()
    request.current = controller
    setPending(true)
    setError("")
    let preparedId = ""
    let timedOut = false
    const timeout = window.setTimeout(() => {
      timedOut = true
      controller.abort("timeout")
    }, 90_000)

    try {
      if (!desktopProfile) {
        setAnswer(await askPaper(paper.id, question, "zh", { signal: controller.signal }))
        return
      }
      if (requestId && requestGeneration) {
        const expectedOperation = operationIdentityRef.current
        if (!expectedOperation) throw new Error("原请求身份无法核对，请重新载入工作区后检查。")
        const operation = await fetchResearchOperation(
          requestId,
          requestGeneration,
          controller.signal,
        )
        if (controller.signal.aborted) return
        await applyOperation(
          operation,
          expectedOwner,
          expectedRevision,
          expectedRunId,
          expectedOperation,
          expectedSelection,
          operationPersonalMemoryRef.current,
        )
        return
      }

      const identity = await fetchDesktopIdentity(controller.signal)
      if (scopeChanged()) return
      const selection = expectedSelection &&
        expectedSelection.owner === expectedOwner &&
        expectedSelection.sourceRevision === expectedRevision
        ? expectedSelection.selection
        : activeSection
          ? { sectionId: activeSection }
          : {}
      const memorySelection = await resolveReaderPersonalMemory(
        personalMemory,
        [
          question,
          paper.title,
          paper.abstractSnippet,
          expectedSelection?.selection.selectedText,
          activeSection,
        ],
        controller.signal,
      )
      if (scopeChanged()) return
      operationPersonalMemoryRef.current = memorySelection
      const semantic = {
        kind: "ask" as const,
        runId: runId!,
        paperId: paper.id,
        sourceRevision: expectedRevision,
        sourceFingerprint: sourceFingerprint!,
        question,
        selection,
        locale: "zh",
        ...(memorySelection ? { personalMemory: memorySelection } : {}),
      }
      const operation = await prepareResearchOperation({
        kind: "ask",
        generation: identity.generation,
        semantic,
        paperId: paper.id,
        sourceRevision: expectedRevision,
        question,
      })
      if (scopeChanged()) return
      operationIdentityRef.current = operation
      preparedId = operation.requestId
      setRequestId(preparedId)
      setRequestGeneration(operation.generation)
      setOperationStatus("waiting")
      updateResearchOperation(preparedId, { status: "waiting" })
      taskActivityRef.current?.({
        kind: "ask",
        requestId: preparedId,
        generation: operation.generation,
        status: "waiting",
        usedPersonalMemory: [],
      })
      const submitted = await submitResearchAsk(
        buildAskRequest(operation, semantic),
        controller.signal,
      )
      if (controller.signal.aborted) return
      await applyOperation(
        submitted,
        expectedOwner,
        expectedRevision,
        expectedRunId,
        operation,
        expectedSelection,
        memorySelection,
      )
    } catch (cause) {
      if (identityChanged()) return
      if (preparedId) {
        updateResearchOperation(preparedId, { status: "unknown" })
        setOperationStatus("unknown")
      }
      setError(
        timedOut
          ? "等待已停止，任务结果仍需按请求身份核对。"
          : controller.signal.aborted
            ? desktopProfile
              ? "已停止等待，后端任务没有被标记为取消。"
              : "已停止回答，问题已保留。"
            : cause instanceof Error
              ? desktopProfile
                ? cause.message
                : `${cause.message}，问题已保留，可重试。`
              : "问答结果暂时无法确认，问题已保留。",
      )
    } finally {
      window.clearTimeout(timeout)
      if (request.current === controller) {
        request.current = null
        if (
          ownerRef.current === expectedOwner &&
          sourceRevisionRef.current === expectedRevision &&
          runIdRef.current === runId
        ) setPending(false)
      }
    }
  }


  function stopWaiting() {
    request.current?.abort("user")
    request.current = null
    setPending(false)
    setError(desktopProfile ? "已停止等待，后端任务没有被标记为取消；稍后可按请求身份继续核对。" : "已停止回答，问题已保留。")
  }

  function exportNote() {
    const content = `# ${paper.title}\n\n${safeReaderSourceUrl(paper.pdfUrl) || ""}\n\n## 阅读笔记\n\n${notebook.note}\n`
    const filename = `${paper.id.replace(/[^a-zA-Z0-9_-]/g, "-")}-notes.md`
    if (onExportNote) {
      void onExportNote({ filename, content })
      return
    }
    const url = URL.createObjectURL(new Blob([content], { type: "text/markdown;charset=utf-8" }))
    const link = document.createElement("a")
    link.href = url
    link.download = filename
    link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  return (
    <aside className={styles.assistant} aria-label="阅读助手">
      <div className={styles.assistantHeading}>
        <span className={styles.spark}><Sparkles size={19} /></span>
        <div><strong>阅读助手</strong><p>理解、追问，留下你的思考</p></div>
      </div>
      <div className={styles.assistantTabs} role="group" aria-label="助手工具">
        <button type="button" aria-pressed={tab === "ask"} onClick={() => setTab("ask")}>
          <Sparkles size={15} />问论文
        </button>
        <button type="button" aria-pressed={tab === "notes"} onClick={() => setTab("notes")}>
          <FilePenLine size={15} />阅读笔记
          {notebook.note && <span className={styles.noteDot} aria-label="有笔记" />}
        </button>
      </div>

      <div hidden={tab !== "ask"} className={styles.askPanel}>
        <div className={styles.assistantContent}>
          {answer ? (
            <div className={styles.answer} ref={replyRef} tabIndex={-1}>
              <p className={styles.askedQuestion}><Quote size={15} />{answer.question}</p>
              <ReactMarkdown>{answer.answer}</ReactMarkdown>
              {answer.executionTrace && (
                <section className={styles.evidence} aria-label="回答执行追溯">
                  <details>
                    <summary>回答执行追溯</summary>
                    <dl>
                      <dt>运行 ID</dt>
                      <dd><code>{answer.executionTrace.runId}</code></dd>
                      <dt>来源修订</dt>
                      <dd><code>{answer.executionTrace.sourceRevision}</code></dd>
                      <dt>Transcript ID</dt>
                      <dd><code>{answer.executionTrace.transcriptId}</code></dd>
                      <dt>回答状态</dt>
                      <dd><code>{answer.executionTrace.answerStatus}</code></dd>
                      <dt>证据 ID</dt>
                      <dd>
                        <ul>
                          {answer.executionTrace.evidenceRefs.map(evidenceId => (
                            <li key={evidenceId}><code>{evidenceId}</code></li>
                          ))}
                        </ul>
                      </dd>
                    </dl>
                  </details>
                </section>
              )}
              <div className={styles.evidence}>
                <strong>回答依据 · {answer.citations.length} 条</strong>
                {answer.citations.length ? answer.citations.map(citation => (
                  <details key={citation.id}>
                    <summary>{citation.label}</summary>
                    {citation.textExcerpt && <blockquote>{citation.textExcerpt}</blockquote>}
                    {(desktopProfile
                      ? citation.valid &&
                        citation.sourceRevision === sourceRevision &&
                        ((citation.sectionId && sectionIds.has(citation.sectionId)) || citation.page)
                      : (citation.sectionId && sectionIds.has(citation.sectionId)) || citation.page) ? (
                      <button type="button" onClick={() => onNavigateCitation(citation)}>
                        {desktopProfile ? "定位原文" : "定位章节"} <BookOpen size={14} />
                      </button>
                    ) : (
                      <small>{citation.reasonCode ? `定位不可用：${citation.reasonCode}` : "当前来源修订无法精确定位，请结合原文核对。"}</small>
                    )}
                  </details>
                )) : <p>本次回答未附来源，请结合原文核对。</p>}
              </div>
            </div>
          ) : (
            <>
              <div className={styles.assistantIntro}>
                <h2>带着问题，读得更深。</h2>
                <p>从一个具体问题开始，回答后可以展开依据，回到原文核对。</p>
              </div>
              <div className={styles.suggestions}>
                <span>你可以这样问</span>
                {prompts.map((prompt, index) => (
                  <button
                    type="button"
                    key={prompt}
                    disabled={!notebook.ready || pending}
                    onClick={() => {
                      notebook.update({ question: prompt })
                      questionRef.current?.focus()
                    }}
                  >
                    <span>0{index + 1}</span>{prompt}
                  </button>
                ))}
              </div>
              {researchQuestion && (
                <button
                  type="button"
                  className={styles.researchQuestion}
                  disabled={!notebook.ready || pending}
                  onClick={() => {
                    notebook.update({ question: researchQuestion })
                    questionRef.current?.focus()
                  }}
                >
                  <small>继续你的研究问题</small>{researchQuestion}
                </button>
              )}
            </>
          )}
        </div>

        <form className={styles.askForm} onSubmit={event => { event.preventDefault(); void submit() }}>
          <label htmlFor="paper-reader-question">向这篇论文提问</label>
          {selectionRequest?.owner === owner && selectionRequest?.sourceRevision === sourceRevision && (
            <p className={styles.unavailableAsk} role="status">
              已绑定所选原文；确认问题后再发送，不会自动运行。
            </p>
          )}
          {unavailableMessage && (
            <p className={styles.unavailableAsk} role="status">{unavailableMessage} 问题可以先保留。</p>
          )}
          <div className={styles.questionInput}>
            <textarea
              id="paper-reader-question"
              ref={questionRef}
              value={notebook.question}
              disabled={!notebook.ready || pending}
              maxLength={2000}
              placeholder="例如：这个方法适合什么场景？"
              onChange={event => notebook.update({ question: event.target.value })}
            />
            <div>
              <span>{pending ? "正在核对真实运行状态…" : requestId ? `请求 ${requestId.slice(0, 8)}…` : "仅围绕当前论文"}</span>
              {pending ? (
                <button type="button" aria-label={desktopProfile ? "停止等待" : "停止回答"} onClick={stopWaiting}><Square size={15} /></button>
              ) : (
                <button
                  type="submit"
                  aria-label={requestId ? "检查已有问答请求" : "发送问题"}
                  disabled={!notebook.ready || !notebook.question.trim() || Boolean(unavailableMessage)}
                >
                  <ArrowUp size={19} />
                </button>
              )}
            </div>
          </div>
          {error && <p role="alert" className={styles.error}>{error}</p>}
          {requestId && ["failed", "rejected", "needs_action"].includes(operationStatus) && (
            <button
              type="button"
              className={styles.exportButton}
              onClick={() => {
                setRequestId("")
                setRequestGeneration("")
                setOperationStatus("")
                setError("")
              }}
            >
              使用新请求身份重新提问
            </button>
          )}
          <p className={styles.footnote}>AI 回答仅供辅助阅读，请核对引用原文。</p>
        </form>
      </div>

      <div hidden={tab !== "notes"} className={styles.notesPanel}>
        <label htmlFor="paper-reader-note">我的阅读笔记</label>
        <p>记录理解、疑问和下一步想验证的事。</p>
        <textarea
          id="paper-reader-note"
          value={notebook.note}
          disabled={!notebook.ready}
          maxLength={20000}
          aria-describedby="paper-reader-note-limit"
          placeholder="这篇论文对我的研究有什么启发？"
          onChange={event => notebook.update({ note: event.target.value })}
        />
        <p
          id="paper-reader-note-limit"
          className={notebook.noteOverLimit ? styles.error : styles.footnote}
        >
          {notebook.noteOverLimit
            ? "这份已有笔记超过 20,000 个字符，内容已完整保留；请缩短后再保存。"
            : "最多 20,000 个字符。"}
        </p>
        <p
          role="status"
          className={notebook.storageError || notebook.saveStatus === "conflict" ? styles.error : styles.footnote}
        >
          {!notebook.ready
            ? "正在读取笔记…"
            : notebook.noteOverLimit
              ? "已有笔记超过保存上限，当前内容不会被覆盖；请缩短或先导出。"
              : notebook.revisionChanged
              ? "论文来源已更新，笔记已保留；章节与页码定位已安全重置。"
              : notebook.saveStatus === "conflict"
                ? "另一标签页已更新工作区。本地笔记仍保留，请先处理同步冲突。"
                : notebook.storageError
                  ? desktopProfile
                    ? "笔记尚未保存，请导出后再离开。"
                    : "浏览器未能保存，请导出笔记后再离开。"
                  : notebook.saveStatus === "synced"
                    ? "已保存到你的研究工作区"
                    : notebook.saveStatus === "saving"
                      ? "正在保存到研究工作区…"
                      : "保存在当前浏览器"}
        </p>
        {notebook.legacyAvailable && (
          <button type="button" className={styles.exportButton} onClick={() => notebook.importLegacy()}>
            导入此浏览器的旧笔记
          </button>
        )}
        <button type="button" className={styles.exportButton} disabled={!notebook.note.trim()} onClick={exportNote}>
          <Download size={16} />导出 Markdown
        </button>
      </div>
    </aside>
  )
}

export function answerFromResult(
  value: unknown,
  paperId: string,
  question: string,
  runId: string | undefined,
  sourceRevision: string,
): PaperReaderAnswer | null {
  const parsed = groundedAnswerSchema.safeParse(value)
  if (
    !parsed.success ||
    !runId ||
    parsed.data.runId !== runId ||
    parsed.data.paperId !== paperId ||
    parsed.data.sourceRevision !== sourceRevision
  ) return null
  return {
    paperId: parsed.data.paperId,
    question,
    answer: parsed.data.answer,
    citations: parsed.data.citations.map(citation => ({
      id: citation.id,
      label: citation.label,
      sourceType: citation.sectionId ? "section" : "evidence",
      sectionId: citation.sectionId,
      page: citation.page,
      sourceRevision: parsed.data.sourceRevision,
      valid: citation.valid,
      reasonCode: citation.reasonCode ?? undefined,
      evidenceId: citation.evidenceId,
      textExcerpt: citation.excerpt,
    })),
    executionTrace: {
      runId: parsed.data.runId,
      sourceRevision: parsed.data.sourceRevision,
      transcriptId: parsed.data.transcriptId,
      answerStatus: parsed.data.answerStatus,
      evidenceRefs: [...parsed.data.evidenceRefs],
    },
    confidence: parsed.data.confidence,
    generatedAt: parsed.data.generatedAt ?? new Date().toISOString(),
    cached: false,
    locale: "zh",
  }
}



export function recoverSelectionRequest(
  operation: ResearchPendingOperation,
  owner: string,
): ReaderSelectionAskRequest | undefined {
  if (operation.kind !== "ask" || operation.semantic.kind !== "ask") return undefined
  const selectedText = operation.semantic.selection.selectedText
  const sectionId = operation.semantic.selection.sectionId
  if (!selectedText || !sectionId) return undefined
  return {
    requestKey: operation.requestId,
    selectionId: "",
    mode: inferSelectionMode(operation.semantic.question),
    question: operation.semantic.question,
    selection: {
      sectionId,
      ...(operation.semantic.selection.page !== undefined
        ? { page: operation.semantic.selection.page }
        : {}),
      selectedText,
    },
    owner,
    sourceRevision: operation.sourceRevision ?? operation.semantic.sourceRevision,
  }
}

function inferSelectionMode(question: string): "explain" | "example" {
  return /例子|示例|example/i.test(question) ? "example" : "explain"
}
