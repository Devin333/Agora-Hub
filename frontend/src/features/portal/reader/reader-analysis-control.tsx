"use client"

import { useEffect, useRef, useState } from "react"
import { LoaderCircle, RefreshCw, Square } from "lucide-react"
import type { Paper } from "@/lib/papers/types"
import {
  fetchDesktopIdentity,
  fetchResearchOperation,
  submitResearchAnalyze,
} from "@/lib/research/desktop/client"
import type { ResearchOperation } from "@/lib/research/desktop/contracts"
import { buildAnalyzeRequest, normalizeAnalyzeTransportUrl } from "@/lib/research/desktop/request-contracts"
import {
  operationReceiptMatches,
  prepareResearchOperation,
  recoverableResearchOperations,
  updateResearchOperation,
  type OperationReceiptIdentity,
} from "@/lib/research/pending-operations"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"

export function ReaderAnalysisControl({
  paper,
  sourceRevision,
  sourceFingerprint,
  sourceUrl,
  pdfUrl,
  initialRunId,
  enabled,
  onRun,
}: {
  paper: Paper
  sourceRevision: string
  sourceFingerprint?: string
  sourceUrl?: string
  pdfUrl?: string
  initialRunId?: string
  enabled: boolean
  onRun: (runId: string) => void | Promise<void>
}) {
  const workspace = useOwnedResearchWorkspace()
  const owner = workspace.owner
  const ownerRef = useRef(owner)
  const scopeRef = useRef("")
  ownerRef.current = owner
  scopeRef.current = `${owner ?? ""}:${paper.id}:${sourceRevision}:${sourceFingerprint ?? ""}`
  const [operation, setOperation] = useState<ResearchOperation | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const active = useRef<AbortController | null>(null)

  useEffect(() => {
    active.current?.abort()
    setBusy(false)
    setError("")
    const saved = recoverableResearchOperations("analyze").find(
      item =>
        item.paperId === paper.id &&
        item.sourceRevision === sourceRevision &&
        item.semantic.kind === "analyze" &&
        item.semantic.sourceFingerprint === sourceFingerprint,
    )
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
    setOperation(next)
    updateResearchOperation(next.requestId, {
      status: next.status,
      ...(next.runId ? { runId: next.runId } : {}),
      ...(next.errorCode ? { errorCode: next.errorCode } : {}),
    })
    if (next.status === "passed" && next.runId) await onRun(next.runId)
    if (["failed", "rejected", "needs_action"].includes(next.status)) {
      setError(next.message || "分析结果需要核对，不能作为已接受报告。")
    }
  }

  async function start() {
    const expectedOwner = owner
    const expectedScope = scopeRef.current
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
      if (ownerRef.current !== expectedOwner || scopeRef.current !== expectedScope) return
      const semantic = {
        kind: "analyze" as const,
        paperId: paper.id,
        sourceRevision,
        sourceFingerprint: sourceFingerprint!,
        sourceUrl: normalizeAnalyzeTransportUrl(sourceUrl),
        pdfUrl: normalizeAnalyzeTransportUrl(pdfUrl),
        objective: `Analyze paper ${paper.id}`,
        constraints: [],
      }
      const prepared = await prepareResearchOperation({
        kind: "analyze",
        generation: identity.generation,
        semantic,
        paperId: paper.id,
        sourceRevision,
      })
      if (ownerRef.current !== expectedOwner || scopeRef.current !== expectedScope) return
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
      const response = await submitResearchAnalyze(
        buildAnalyzeRequest(prepared, semantic),
        controller.signal,
      )
      await apply(response, expectedOwner, expectedScope, prepared)
    } catch (cause) {
      if (ownerRef.current !== expectedOwner || scopeRef.current !== expectedScope) return
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
      await apply(response, expectedOwner, expectedScope, operation)
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
    <section className="mb-3 rounded-xl border border-[#e4d9ef] bg-white/80 p-3 text-sm">
      <strong>Research 分析</strong>
      <p className="mt-1 text-[#756782]">报告只来自通过质量门的真实分析运行。</p>
      {!exactSource && (
        <p className="mt-2 text-amber-800">来源修订身份尚未确认，当前不能开始分析。</p>
      )}
      {runId ? (
        <a
          className="mt-2 inline-block text-[#6d28d9] underline"
          href={`/reports/runs/${encodeURIComponent(runId)}?sourceRevision=${encodeURIComponent(sourceRevision)}`}
        >
          查看本次运行报告
        </a>
      ) : operation ? (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <span role="status">{analysisStatus(operation.status)}</span>
          {terminal ? (
            <button
              type="button"
              className="underline"
              onClick={() => {
                setOperation(null)
                setError("")
              }}
            >
              使用新请求身份重试
            </button>
          ) : (
            <button
              type="button"
              disabled={busy}
              className="inline-flex items-center gap-1 underline"
              onClick={() => void check()}
            >
              {busy ? <LoaderCircle size={14} className="animate-spin" /> : <RefreshCw size={14} />}
              检查已有请求
            </button>
          )}
        </div>
      ) : (
        <button
          type="button"
          disabled={!enabled || !exactSource || busy}
          className="mt-2 rounded-lg bg-[#6d28d9] px-3 py-2 text-white disabled:opacity-50"
          onClick={() => void start()}
        >
          {busy ? (
            <><LoaderCircle size={14} className="mr-1 inline animate-spin" />正在提交</>
          ) : (
            "开始真实分析"
          )}
        </button>
      )}
      {busy && (
        <button
          type="button"
          aria-label="停止等待分析"
          className="ml-2"
          onClick={() => active.current?.abort("user")}
        >
          <Square size={14} />
        </button>
      )}
      {error && <p role="alert" className="mt-2 text-[#a02b47]">{error}</p>}
    </section>
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
