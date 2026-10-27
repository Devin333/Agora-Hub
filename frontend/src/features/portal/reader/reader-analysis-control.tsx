"use client"

import { useEffect, useRef, useState } from "react"
import { ArrowUpRight, ChartNoAxesColumn, LoaderCircle, RefreshCw, Square } from "lucide-react"
import { Button } from "@/components/ui/button"
import type { Paper } from "@/lib/papers/types"
import { ReaderTaskCard, ReaderTaskStatus } from "./reader-task-card"
import {
  fetchDesktopIdentity,
  fetchResearchOperation,
  submitResearchAnalyze,
} from "@/lib/research/desktop/client"
import type { ResearchOperation } from "@/lib/research/desktop/contracts"
import type { ResearchPersonalMemorySelection } from "@/lib/research/workspace-items"
import { buildAnalyzeRequest, normalizeAnalyzeTransportUrl } from "@/lib/research/desktop/request-contracts"
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
  usedPersonalMemoryIsCompatible,
  usedPersonalMemoryMatches,
  type ReaderPersonalMemoryControl,
  type ReaderTaskActivity,
} from "./personal-memory-task-context"


export type ReaderAnalysisControlProps = {
  paper: Paper
  sourceRevision: string
  sourceFingerprint?: string
  sourceUrl?: string
  pdfUrl?: string
  initialRunId?: string
  enabled: boolean
  onRun: (runId: string) => void | Promise<void>
  personalMemory?: ReaderPersonalMemoryControl
  onTaskActivity?: (activity: ReaderTaskActivity) => void
}

export function ReaderAnalysisControl({
  paper,
  sourceRevision,
  sourceFingerprint,
  sourceUrl,
  pdfUrl,
  initialRunId,
  enabled,
  onRun,
  personalMemory,
  onTaskActivity,
}: ReaderAnalysisControlProps) {
  const workspace = useOwnedResearchWorkspace()
  const owner = workspace.owner
  const ownerRef = useRef(owner)
  const scopeRef = useRef("")
  const personalMemoryRef = useRef(personalMemory)
  const taskActivityRef = useRef(onTaskActivity)
  const operationPersonalMemoryRef = useRef<ResearchPersonalMemorySelection | undefined>(undefined)
  ownerRef.current = owner
  scopeRef.current = `${owner ?? ""}:${paper.id}:${sourceRevision}:${sourceFingerprint ?? ""}`
  personalMemoryRef.current = personalMemory
  taskActivityRef.current = onTaskActivity
  const [operation, setOperation] = useState<ResearchOperation | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const active = useRef<AbortController | null>(null)

  useEffect(() => {
    active.current?.abort()
    setBusy(false)
    setError("")
    operationPersonalMemoryRef.current = undefined
    const saved = recoverableResearchOperations("analyze").find(
      item =>
        item.paperId === paper.id &&
        item.sourceRevision === sourceRevision &&
        item.semantic.kind === "analyze" &&
        item.semantic.sourceFingerprint === sourceFingerprint,
    )
    if (saved?.semantic.kind === "analyze") {
      operationPersonalMemoryRef.current = saved.semantic.personalMemory
    }
    setOperation(
      saved
        ? {
            requestId: saved.requestId,
            generation: saved.generation,
            kind: "analyze",
            fingerprint: saved.fingerprint,
            status:
              saved.status === "prepared"
                ? "waiting"
                : saved.status === "interrupted"
                  ? "unknown"
                  : saved.status,
            updatedAt: new Date(saved.updatedAt).toISOString(),
            paperId: saved.paperId,
            sourceRevision: saved.sourceRevision,
            runId: saved.runId,
            errorCode: saved.errorCode,
            recoverable: true,
            automaticRetryAllowed: false,
          }
        : null,
    )
    return () => active.current?.abort()
  }, [owner, paper.id, sourceFingerprint, sourceRevision])

  async function apply(
    next: ResearchOperation,
    expectedOwner: string,
    expectedScope: string,
    expectedOperation: OperationReceiptIdentity,
    expectedPersonalMemory?: ResearchPersonalMemorySelection,
  ) {
    if (ownerRef.current !== expectedOwner || scopeRef.current !== expectedScope) return
    if (!operationReceiptMatches(next, expectedOperation)) {
      updateResearchOperation(expectedOperation.requestId, { status: "unknown" })
      setOperation({
        ...expectedOperation,
        status: "unknown",
        updatedAt: new Date().toISOString(),
        paperId: paper.id,
        sourceRevision,
        errorCode: "operation_identity_mismatch",
        recoverable: true,
        automaticRetryAllowed: false,
      })
      setError("分析回执与已保存的请求身份不一致，请检查原请求，不能采用该结果。")
      return
    }
    const memoryDisclosureMatches = next.status === "passed"
      ? usedPersonalMemoryMatches(expectedPersonalMemory, next.result)
      : usedPersonalMemoryIsCompatible(expectedPersonalMemory, next.result)
    if (!memoryDisclosureMatches) {
      updateResearchOperation(expectedOperation.requestId, { status: "unknown" })
      setOperation({
        ...next,
        status: "unknown",
        errorCode: "personal_memory_disclosure_mismatch",
      })
      setError("分析返回的个人记忆披露与原请求快照不一致，不能采用该结果。")
      taskActivityRef.current?.({
        kind: "analyze",
        requestId: next.requestId,
        generation: next.generation,
        status: "unknown",
        usedPersonalMemory: [],
      })
      return
    }
    if (
      next.status === "passed" &&
      !acceptedAnalysisResultMatches(next, paper.id, sourceRevision)
    ) {
      updateResearchOperation(expectedOperation.requestId, { status: "unknown" })
      setOperation({
        ...next,
        status: "unknown",
        errorCode: "analysis_result_not_accepted",
      })
      setError("分析结果的运行、来源或确定性质量门回执无效，不能作为已接受报告。")
      taskActivityRef.current?.({
        kind: "analyze",
        requestId: next.requestId,
        generation: next.generation,
        status: "unknown",
        usedPersonalMemory: [],
      })
      return
    }
    setOperation(next)
    updateResearchOperation(next.requestId, {
      status: next.status,
      ...(next.runId ? { runId: next.runId } : {}),
      ...(next.errorCode ? { errorCode: next.errorCode } : {}),
    })
    taskActivityRef.current?.({
      kind: "analyze",
      requestId: next.requestId,
      generation: next.generation,
      status: next.status,
      usedPersonalMemory: usedPersonalMemoryFromResult(next.result),
    })
    if (next.status === "passed" && next.runId) await onRun(next.runId)
    if (["failed", "rejected", "needs_action"].includes(next.status)) {
      setError(next.message || "分析结果需要核对，不能作为已接受报告。")
    }
  }

  async function start() {
    const expectedOwner = owner
    const expectedScope = scopeRef.current
    const expectedMemoryEnabled = personalMemory?.enabled === true
    const memoryControlChanged = () =>
      (personalMemoryRef.current?.enabled === true) !== expectedMemoryEnabled
    if (
      !expectedOwner ||
      busy ||
      !hasExactSourceIdentity(sourceRevision, sourceFingerprint)
    ) {
      return
    }

    const controller = new AbortController()
    active.current = controller
    setBusy(true)
    setError("")
    let preparedView: ResearchOperation | null = null
    try {
      const identity = await fetchDesktopIdentity(controller.signal)
      if (
        ownerRef.current !== expectedOwner ||
        scopeRef.current !== expectedScope ||
        memoryControlChanged() ||
        controller.signal.aborted
      ) return
      const objective = `Analyze paper ${paper.id}`
      const memorySelection = await resolveReaderPersonalMemory(
        personalMemory,
        [objective, paper.title, paper.abstractSnippet, ...paper.tags],
        controller.signal,
      )
      if (
        ownerRef.current !== expectedOwner ||
        scopeRef.current !== expectedScope ||
        memoryControlChanged() ||
        controller.signal.aborted
      ) return
      operationPersonalMemoryRef.current = memorySelection
      const semantic = {
        kind: "analyze" as const,
        paperId: paper.id,
        sourceRevision,
        sourceFingerprint: sourceFingerprint!,
        sourceUrl: normalizeAnalyzeTransportUrl(sourceUrl),
        pdfUrl: normalizeAnalyzeTransportUrl(pdfUrl),
        objective,
        constraints: [],
        ...(memorySelection ? { personalMemory: memorySelection } : {}),
      }
      const prepared = await prepareResearchOperation({
        kind: "analyze",
        generation: identity.generation,
        semantic,
        paperId: paper.id,
        sourceRevision,
      })
      if (
        ownerRef.current !== expectedOwner ||
        scopeRef.current !== expectedScope ||
        memoryControlChanged() ||
        controller.signal.aborted
      ) return
      preparedView = {
        requestId: prepared.requestId,
        generation: prepared.generation,
        kind: "analyze",
        fingerprint: prepared.fingerprint,
        status: "waiting",
        updatedAt: new Date(prepared.updatedAt).toISOString(),
        paperId: paper.id,
        sourceRevision,
        recoverable: true,
        automaticRetryAllowed: false,
      }
      setOperation(preparedView)
      updateResearchOperation(prepared.requestId, { status: "waiting" })
      taskActivityRef.current?.({
        kind: "analyze",
        requestId: prepared.requestId,
        generation: prepared.generation,
        status: "waiting",
        usedPersonalMemory: [],
      })
      if (memoryControlChanged() || controller.signal.aborted) return
      const response = await submitResearchAnalyze(
        buildAnalyzeRequest(prepared, semantic),
        controller.signal,
      )
      await apply(
        response,
        expectedOwner,
        expectedScope,
        prepared,
        memorySelection,
      )
    } catch (cause) {
      if (
        ownerRef.current !== expectedOwner ||
        scopeRef.current !== expectedScope ||
        memoryControlChanged()
      ) return
      if (preparedView) {
        const uncertain = { ...preparedView, status: "unknown" as const }
        setOperation(uncertain)
        updateResearchOperation(uncertain.requestId, { status: "unknown" })
      }
      setError(
        controller.signal.aborted
          ? "已停止等待；分析任务没有被标记为取消，可按请求身份继续核对。"
          : cause instanceof Error
            ? cause.message
            : "分析结果暂时无法确认。",
      )
    } finally {
      if (active.current === controller) {
        active.current = null
        if (ownerRef.current === expectedOwner && scopeRef.current === expectedScope) {
          setBusy(false)
        }
      }
    }
  }

  async function check() {
    const expectedOwner = owner
    const expectedScope = scopeRef.current
    if (!expectedOwner || !operation || busy) return
    const controller = new AbortController()
    active.current = controller
    setBusy(true)
    setError("")
    try {
      const response = await fetchResearchOperation(
        operation.requestId,
        operation.generation,
        controller.signal,
      )
      await apply(
        response,
        expectedOwner,
        expectedScope,
        operation,
        operationPersonalMemoryRef.current,
      )
    } catch (cause) {
      if (
        ownerRef.current === expectedOwner &&
        scopeRef.current === expectedScope &&
        !controller.signal.aborted
      ) {
        setError(cause instanceof Error ? cause.message : "分析状态暂时无法确认。")
      }
    } finally {
      if (active.current === controller) {
        active.current = null
        if (ownerRef.current === expectedOwner && scopeRef.current === expectedScope) {
          setBusy(false)
        }
      }
    }
  }

  const exactSource = hasExactSourceIdentity(sourceRevision, sourceFingerprint)
  const runId = initialRunId || (operation?.status === "passed" ? operation.runId : undefined)
  const terminal = operation && ["failed", "rejected", "needs_action"].includes(operation.status)

  return (
    <ReaderTaskCard
      icon={ChartNoAxesColumn}
      title="Research 分析"
      description="报告只来自通过质量门的真实分析运行。"
      warning={exactSource ? undefined : "来源修订身份尚未确认，当前不能开始分析。"}
      error={error}
    >
      {runId ? (
        <a
          className="inline-flex items-center gap-1 text-body2 text-link underline-offset-4 hover:underline"
          href={`/reports/runs/${encodeURIComponent(runId)}?sourceRevision=${encodeURIComponent(sourceRevision)}`}
        >
          查看本次运行报告
          <ArrowUpRight size={14} strokeWidth={1.8} aria-hidden="true" />
        </a>
      ) : operation ? (
        <>
          <ReaderTaskStatus>{analysisStatus(operation.status)}</ReaderTaskStatus>
          {terminal ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => {
                setOperation(null)
                operationPersonalMemoryRef.current = undefined
                setError("")
              }}
            >
              使用新请求身份重试
            </Button>
          ) : (
            <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void check()}>
              {busy ? <LoaderCircle size={14} strokeWidth={1.8} className="animate-spin" /> : <RefreshCw size={14} strokeWidth={1.8} />}
              检查已有请求
            </Button>
          )}
        </>
      ) : (
        <Button type="button" size="sm" disabled={!enabled || !exactSource || busy} onClick={() => void start()}>
          {busy ? (
            <><LoaderCircle size={14} strokeWidth={1.8} className="animate-spin" />正在提交</>
          ) : (
            "开始真实分析"
          )}
        </Button>
      )}
      {busy && (
        <Button
          type="button"
          variant="outline"
          size="icon-sm"
          className="rounded-full bg-muted text-foreground/80 hover:bg-accent"
          aria-label="停止等待分析"
          onClick={() => active.current?.abort("user")}
        >
          <Square size={10} strokeWidth={0} fill="currentColor" />
        </Button>
      )}
    </ReaderTaskCard>
  )
}

function acceptedAnalysisResultMatches(
  operation: ResearchOperation,
  paperId: string,
  sourceRevision: string,
): boolean {
  if (!operation.result || typeof operation.result !== "object" || Array.isArray(operation.result)) {
    return false
  }
  const result = operation.result as Record<string, unknown>
  return (
    typeof operation.runId === "string" &&
    operation.runId.length > 0 &&
    result.runId === operation.runId &&
    result.paperId === paperId &&
    result.sourceRevision === sourceRevision &&
    result.status === "succeeded" &&
    result.accepted === true &&
    result.qualityPassed === true &&
    result.eligibleForAcceptedExport === true &&
    result.artifactStatus === "valid"
  )
}

function hasExactSourceIdentity(sourceRevision: string, sourceFingerprint?: string): boolean {
  return (
    Boolean(sourceFingerprint) &&
    /^sha256:[0-9a-f]{64}$/.test(sourceFingerprint ?? "") &&
    Boolean(sourceRevision) &&
    !sourceRevision.startsWith("unknown:")
  )
}

function analysisStatus(status: ResearchOperation["status"]) {
  if (status === "failed") return "分析失败"
  if (status === "rejected") return "分析未通过质量门"
  if (status === "needs_action") return "分析需要人工核对"
  if (status === "interrupted") return "分析运行已中断，需要按原身份核对"
  if (status === "unknown") return "请求结果未知，需要按原身份查询"
  if (status === "running") return "分析正在运行"
  return "分析已接收，等待真实运行结果"
}
