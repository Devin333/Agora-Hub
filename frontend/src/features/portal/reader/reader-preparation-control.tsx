"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { FileSearch, LoaderCircle, RefreshCw, Square } from "lucide-react"
import { Button } from "@/components/ui/button"
import type { Paper } from "@/lib/papers/types"
import { ReaderTaskCard, ReaderTaskStatus } from "./reader-task-card"
import {
  fetchDesktopIdentity,
  fetchResearchOperation,
  submitResearchPrepare,
} from "@/lib/research/desktop/client"
import type { ResearchOperation } from "@/lib/research/desktop/contracts"
import {
  buildPrepareRequest,
  normalizeAnalyzeTransportUrl,
} from "@/lib/research/desktop/request-contracts"
import {
  operationReceiptMatches,
  prepareResearchOperation,
  recoverableResearchOperations,
  updateResearchOperation,
  type OperationReceiptIdentity,
} from "@/lib/research/pending-operations"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"

export function ReaderPreparationControl({
  paper,
  sourceUrl,
  enabled,
  onReady,
}: {
  paper: Paper
  sourceUrl?: string
  enabled: boolean
  onReady: () => void | Promise<void>
}) {
  const workspace = useOwnedResearchWorkspace()
  const owner = workspace.owner
  const publicSourceUrl = normalizeAnalyzeTransportUrl(sourceUrl)
  const ownerRef = useRef(owner)
  const scopeRef = useRef("")
  const active = useRef<AbortController | null>(null)
  const paperIdRef = useRef(paper.id)
  const onReadyRef = useRef(onReady)
  ownerRef.current = owner
  paperIdRef.current = paper.id
  onReadyRef.current = onReady
  scopeRef.current = `${owner ?? ""}:${paper.id}:${publicSourceUrl ?? ""}`

  const [operation, setOperation] = useState<ResearchOperation | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  const apply = useCallback(async (
    next: ResearchOperation,
    expectedOwner: string,
    expectedScope: string,
    expectedOperation: OperationReceiptIdentity,
  ) => {
    if (ownerRef.current !== expectedOwner || scopeRef.current !== expectedScope) return
    if (!operationReceiptMatches(next, expectedOperation)) {
      updateResearchOperation(expectedOperation.requestId, { status: "unknown" })
      setOperation({
        ...expectedOperation,
        status: "unknown",
        updatedAt: new Date().toISOString(),
        errorCode: "operation_identity_mismatch",
        recoverable: true,
        automaticRetryAllowed: false,
      })
      setError("正文准备回执与已保存的请求身份不一致，请检查原请求，不能采用该结果。")
      return
    }
    setOperation(next)
    updateResearchOperation(next.requestId, {
      status: next.status,
      ...(next.errorCode ? { errorCode: next.errorCode } : {}),
    })

    if (next.status === "passed") {
      if (!validPreparedSourceResult(next, paperIdRef.current)) {
        setError("正文准备已完成，但返回的来源身份无法核对。请检查运行记录。")
        return
      }
      setError("")
      await onReadyRef.current()
      return
    }

    if (["failed", "rejected", "needs_action"].includes(next.status)) {
      setError(next.message || "正文准备没有完成，请核对来源后使用新的请求身份重试。")
    }
  }, [])


  useEffect(() => {
    active.current?.abort()
    setBusy(false)
    setError("")
    const saved = recoverableResearchOperations("prepare").find(
      item =>
        item.paperId === paper.id &&
        item.semantic.kind === "prepare" &&
        item.semantic.sourceUrl === publicSourceUrl,
    )
    setOperation(saved ? operationFromPending(saved) : null)

    if (!saved || !owner) {
      return () => active.current?.abort()
    }

    const controller = new AbortController()
    active.current = controller
    setBusy(true)
    const expectedOwner = owner
    const expectedScope = scopeRef.current
    void fetchResearchOperation(saved.requestId, saved.generation, controller.signal)
      .then(next => {
        if (!controller.signal.aborted) {
          return apply(next, expectedOwner, expectedScope, saved)
        }
      })
      .catch((cause: unknown) => {
        if (
          !controller.signal.aborted &&
          ownerRef.current === expectedOwner &&
          scopeRef.current === expectedScope
        ) {
          setError(
            cause instanceof Error
              ? cause.message
              : "无法查询已保存的准备请求；系统没有自动重新执行。",
          )
        }
      })
      .finally(() => {
        if (
          !controller.signal.aborted &&
          ownerRef.current === expectedOwner &&
          scopeRef.current === expectedScope
        ) {
          active.current = null
          setBusy(false)
        }
      })

    return () => controller.abort()
  }, [apply, owner, paper.id, publicSourceUrl])


  async function start() {
    const expectedOwner = owner
    const expectedScope = scopeRef.current
    if (!expectedOwner || !publicSourceUrl || busy) return

    const controller = new AbortController()
    active.current = controller
    setBusy(true)
    setError("")
    let preparedView: ResearchOperation | null = null

    try {
      const identity = await fetchDesktopIdentity(controller.signal)
      if (ownerRef.current !== expectedOwner || scopeRef.current !== expectedScope) return
      const semantic = {
        kind: "prepare" as const,
        paperId: paper.id,
        sourceUrl: publicSourceUrl,
        sourceType: null,
      }
      const prepared = await prepareResearchOperation({
        kind: "prepare",
        generation: identity.generation,
        semantic,
        paperId: paper.id,
      })
      if (ownerRef.current !== expectedOwner || scopeRef.current !== expectedScope) return

      preparedView = operationFromPending(prepared)
      setOperation(preparedView)
      updateResearchOperation(prepared.requestId, { status: "waiting" })
      const response = await submitResearchPrepare(
        buildPrepareRequest(prepared, semantic),
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
          ? "已停止等待；正文准备没有被标记为取消，可按原请求身份继续核对。"
          : cause instanceof Error
            ? cause.message
            : "正文准备结果暂时无法确认。",
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
        setError(cause instanceof Error ? cause.message : "正文准备状态暂时无法确认。")
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

  function stopWaiting() {
    active.current?.abort("user")
    active.current = null
    setBusy(false)
    setError("已停止等待；后端操作没有被标记为取消，可按原请求身份继续核对。")
  }

  const terminal = operation && ["failed", "rejected", "needs_action"].includes(operation.status)

  return (
    <ReaderTaskCard
      icon={FileSearch}
      title="准备论文正文"
      description="解析公开来源并建立可核对的来源修订身份，完成后才能分析与问答。"
      warning={publicSourceUrl ? undefined : "当前没有可提交的公开 HTTP(S) 来源；私有导入请从工作区导入入口处理。"}
      error={error}
    >
      {operation ? (
        <>
          <ReaderTaskStatus>{preparationStatus(operation.status)}</ReaderTaskStatus>
          {terminal ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => {
                setOperation(null)
                setError("")
              }}
            >
              使用新请求身份重新准备
            </Button>
          ) : operation.status !== "passed" ? (
            <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void check()}>
              {busy ? <LoaderCircle size={14} strokeWidth={1.8} className="animate-spin" /> : <RefreshCw size={14} strokeWidth={1.8} />}
              检查已有请求
            </Button>
          ) : null}
        </>
      ) : (
        <Button
          type="button"
          size="sm"
          disabled={!enabled || !owner || !publicSourceUrl || busy}
          onClick={() => void start()}
        >
          {busy ? (
            <><LoaderCircle size={14} strokeWidth={1.8} className="animate-spin" />正在提交</>
          ) : (
            "准备正文"
          )}
        </Button>
      )}
      {busy && (
        <Button
          type="button"
          variant="outline"
          size="icon-sm"
          className="rounded-full bg-muted text-foreground/80 hover:bg-accent"
          aria-label="停止等待正文准备"
          onClick={stopWaiting}
        >
          <Square size={10} strokeWidth={0} fill="currentColor" />
        </Button>
      )}
    </ReaderTaskCard>
  )
}

function operationFromPending(operation: {
  requestId: string
  generation: string
  fingerprint: string
  status: "prepared" | "waiting" | "running" | "needs_action" | "passed" | "rejected" | "failed" | "interrupted" | "unknown"
  updatedAt: number
  paperId?: string
  errorCode?: string
}): ResearchOperation {
  return {
    requestId: operation.requestId,
    generation: operation.generation,
    fingerprint: operation.fingerprint,
    kind: "prepare",
    status:
      operation.status === "prepared"
        ? "waiting"
        : operation.status === "interrupted"
          ? "unknown"
          : operation.status,
    updatedAt: new Date(operation.updatedAt).toISOString(),
    paperId: operation.paperId,
    errorCode: operation.errorCode,
    recoverable: true,
    automaticRetryAllowed: false,
  }
}

export function validPreparedSourceResult(
  operation: ResearchOperation,
  paperId: string,
): boolean {
  const result = operation.result
  return Boolean(
    result &&
    result.status === "ready" &&
    result.paperId === paperId &&
    typeof result.sourceRevision === "string" &&
    result.sourceRevision.length > 0 &&
    typeof result.sourceFingerprint === "string" &&
    /^sha256:[0-9a-f]{64}$/.test(result.sourceFingerprint),
  )
}

function preparationStatus(status: ResearchOperation["status"]): string {
  if (status === "passed") return "正文已准备，正在重新读取可核对来源"
  if (status === "failed") return "正文准备失败"
  if (status === "rejected") return "正文准备未通过来源校验"
  if (status === "needs_action") return "正文准备需要人工核对"
  if (status === "interrupted") return "正文准备已中断，需要按原身份核对"
  if (status === "unknown") return "请求结果未知，需要按原身份查询"
  if (status === "running") return "正在解析真实论文来源"
  return "请求已持久化，等待真实解析结果"
}
