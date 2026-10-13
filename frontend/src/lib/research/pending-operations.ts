import {
  historyState,
  researchHistoryEvent,
  savePendingOperation,
} from "@/lib/research/history"
import type { ResearchOperation } from "@/lib/research/desktop/contracts"
import {
  validOperationSemantic,
  type ResearchOperationKind,
  type ResearchOperationSemantic,
  type ResearchOperationStatus,
  type ResearchPendingOperation,
} from "@/lib/research/workspace-items"

export type PreparedOperation = ResearchPendingOperation

export type OperationReceiptIdentity = Pick<
  ResearchPendingOperation,
  "requestId" | "generation" | "kind" | "fingerprint"
>

export function operationReceiptMatches(
  operation: Pick<
    ResearchOperation,
    "requestId" | "generation" | "kind" | "fingerprint"
  >,
  expected: OperationReceiptIdentity,
): boolean {
  return (
    operation.requestId === expected.requestId &&
    operation.generation === expected.generation &&
    operation.kind === expected.kind &&
    operation.fingerprint === expected.fingerprint
  )
}


export async function semanticFingerprint(
  value: ResearchOperationSemantic,
): Promise<string> {
  if (!validOperationSemantic(value)) throw new Error("研究请求语义无效。")
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(canonicalSemanticJson(value)),
  )
  return `sha256:${Array.from(
    new Uint8Array(digest),
    byte => byte.toString(16).padStart(2, "0"),
  ).join("")}`
}

export async function prepareResearchOperation(input: {
  kind: ResearchOperationKind
  generation: string
  semantic: ResearchOperationSemantic
  paperId?: string
  sourceRevision?: string
  question?: string
}): Promise<PreparedOperation> {
  const state = historyState()
  if (!state.owner || !state.ready) {
    throw new Error("请先登录，再提交研究任务。")
  }
  if (input.semantic.kind !== input.kind || !validOperationSemantic(input.semantic, input.kind)) {
    throw new Error("研究请求语义无效。")
  }

  const now = Date.now()
  const operation: PreparedOperation = {
    requestId: crypto.randomUUID(),
    generation: input.generation,
    kind: input.kind,
    fingerprint: await semanticFingerprint(input.semantic),
    semantic: input.semantic,
    status: "prepared",
    ...(input.paperId ? { paperId: input.paperId } : {}),
    ...(input.sourceRevision ? { sourceRevision: input.sourceRevision } : {}),
    ...(input.question ? { question: input.question } : {}),
    createdAt: now,
    updatedAt: now,
  }

  if (!savePendingOperation(operation, state.owner)) {
    throw new Error("未能保存请求身份，请保留当前页面后重试。")
  }
  await waitForResearchWorkspaceSync(state.owner)
  return operation
}

export function updateResearchOperation(
  requestId: string,
  patch: Partial<
    Pick<ResearchPendingOperation, "status" | "importId" | "runId" | "errorCode">
  >,
): boolean {
  const state = historyState()
  const existing = state.workspace.pendingOperations?.find(
    item => item.requestId === requestId,
  )
  if (!existing || !state.owner) return false
  return savePendingOperation(
    { ...existing, ...patch, updatedAt: Date.now() },
    state.owner,
  )
}

export function recoverableResearchOperations(kind?: ResearchOperationKind) {
  return (historyState().workspace.pendingOperations ?? []).filter(
    item =>
      (!kind || item.kind === kind) &&
      !["passed", "rejected", "failed"].includes(item.status),
  )
}

export function operationStatus(value: unknown): ResearchOperationStatus {
  return [
    "prepared",
    "waiting",
    "running",
    "needs_action",
    "passed",
    "rejected",
    "failed",
    "interrupted",
    "unknown",
  ].includes(String(value))
    ? (value as ResearchOperationStatus)
    : "unknown"
}

export function waitForResearchWorkspaceSync(
  expectedOwner: string,
  timeoutMs = 15000,
): Promise<void> {
  const state = historyState()
  if (state.owner !== expectedOwner) {
    return Promise.reject(new Error("账号已切换，请重新提交。"))
  }
  if (!state.dirty && state.status === "synced") return Promise.resolve()

  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(
      () => finish(new Error("请求身份尚未同步，请检查连接后重试。")),
      timeoutMs,
    )

    function changed() {
      const next = historyState()
      if (next.owner !== expectedOwner) {
        finish(new Error("账号已切换，请重新提交。"))
      } else if (!next.dirty && next.status === "synced") {
        finish()
      } else if (next.status === "conflict" || next.status === "error") {
        finish(new Error(next.message || "请求身份同步失败，请重试。"))
      }
    }

    function finish(error?: Error) {
      window.clearTimeout(timeout)
      window.removeEventListener(researchHistoryEvent, changed)
      if (error) reject(error)
      else resolve()
    }

    window.addEventListener(researchHistoryEvent, changed)
    changed()
  })
}

export function canonicalSemanticJson(value: unknown): string {
  return JSON.stringify(sortValue(value))
}

function sortValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortValue)
  if (!value || typeof value !== "object") return value
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
      .map(([key, child]) => [key, sortValue(child)]),
  )
}
