import {
  previewPersonalMemoryTask,
  type PersonalMemoryTaskPreview,
} from "@/lib/research/desktop/client"
import type {
  ResearchPersonalMemoryRef,
  ResearchPersonalMemorySelection,
} from "@/lib/research/workspace-items"

const DEFAULT_RECALL_LIMIT = 5
const MAX_READER_RECALL_LIMIT = 10
const MAX_RECALL_QUERY_LENGTH = 512

export type ReaderPersonalMemoryControl = {
  enabled: boolean
  limit?: number
  onPreview?: (preview: PersonalMemoryTaskPreview) => void
}

export type ReaderTaskActivity = {
  kind: "ask" | "analyze"
  requestId: string
  generation: string
  status: "waiting" | "running" | "needs_action" | "passed" | "rejected" | "failed" | "interrupted" | "unknown"
  usedPersonalMemory: ResearchPersonalMemoryRef[]
}

export async function resolveReaderPersonalMemory(
  control: ReaderPersonalMemoryControl | undefined,
  queryParts: Array<string | null | undefined>,
  signal?: AbortSignal,
): Promise<ResearchPersonalMemorySelection | undefined> {
  if (!control?.enabled) return undefined
  const query = boundedRecallQuery(queryParts)
  if (!query) return undefined
  const preview = await previewPersonalMemoryTask(
    {
      query,
      limit: recallLimit(control.limit),
      useMemory: true,
    },
    signal,
  )
  control.onPreview?.(preview)
  if (preview.reason !== "matched") return undefined
  const selection = normalizedSelection(preview.selection)
  if (
    !selection ||
    preview.policy.policy.allow_remote_processing !== true ||
    preview.policy.policy.revision !== selection.policyRevision
  ) {
    throw new Error("个人记忆召回回执无效，未发送研究任务。")
  }
  return selection
}

export function usedPersonalMemoryMatches(
  expected: ResearchPersonalMemorySelection | undefined,
  result: unknown,
): boolean {
  const parsed = parseUsedPersonalMemory(result)
  if (!parsed.valid) return false
  if (!expected) return !parsed.present || parsed.refs.length === 0
  if (!parsed.present || parsed.refs.length !== expected.refs.length) return false
  return expected.refs.every((ref, index) => {
    const actual = parsed.refs[index]
    return actual?.memoryId === ref.memoryId && actual.revision === ref.revision
  })
}

export function usedPersonalMemoryIsCompatible(
  expected: ResearchPersonalMemorySelection | undefined,
  result: unknown,
): boolean {
  const parsed = parseUsedPersonalMemory(result)
  if (!parsed.valid) return false
  if (!parsed.present) return true
  if (!expected) return parsed.refs.length === 0
  if (parsed.refs.length === 0) return true
  return usedPersonalMemoryMatches(expected, result)
}

export function usedPersonalMemoryFromResult(
  result: unknown,
): ResearchPersonalMemoryRef[] {
  const parsed = parseUsedPersonalMemory(result)
  return parsed.valid ? parsed.refs : []
}

function parseUsedPersonalMemory(result: unknown): {
  present: boolean
  valid: boolean
  refs: ResearchPersonalMemoryRef[]
} {
  if (!result || typeof result !== "object" || Array.isArray(result)) {
    return { present: false, valid: true, refs: [] }
  }
  const record = result as Record<string, unknown>
  if (!("usedPersonalMemory" in record)) {
    return { present: false, valid: true, refs: [] }
  }
  const value = record.usedPersonalMemory
  if (!Array.isArray(value) || value.length > 20) {
    return { present: true, valid: false, refs: [] }
  }
  const seen = new Set<string>()
  const refs: ResearchPersonalMemoryRef[] = []
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      return { present: true, valid: false, refs: [] }
    }
    const ref = item as Record<string, unknown>
    if (
      Object.keys(ref).some(key => key !== "memoryId" && key !== "revision") ||
      typeof ref.memoryId !== "string" ||
      !/^pmem_[0-9a-f]{32}$/.test(ref.memoryId) ||
      seen.has(ref.memoryId) ||
      typeof ref.revision !== "number" ||
      !Number.isSafeInteger(ref.revision) ||
      ref.revision < 1
    ) {
      return { present: true, valid: false, refs: [] }
    }
    seen.add(ref.memoryId)
    refs.push({ memoryId: ref.memoryId, revision: ref.revision })
  }
  return { present: true, valid: true, refs }
}

function normalizedSelection(
  value: ResearchPersonalMemorySelection | undefined,
): ResearchPersonalMemorySelection | undefined {
  if (!value || !Number.isSafeInteger(value.policyRevision) || value.policyRevision < 1) {
    return undefined
  }
  if (!Array.isArray(value.refs) || value.refs.length < 1 || value.refs.length > 20) {
    return undefined
  }
  const seen = new Set<string>()
  const refs: ResearchPersonalMemoryRef[] = []
  for (const ref of value.refs) {
    if (
      !ref ||
      typeof ref.memoryId !== "string" ||
      !/^pmem_[0-9a-f]{32}$/.test(ref.memoryId) ||
      seen.has(ref.memoryId) ||
      !Number.isSafeInteger(ref.revision) ||
      ref.revision < 1
    ) {
      return undefined
    }
    seen.add(ref.memoryId)
    refs.push({ memoryId: ref.memoryId, revision: ref.revision })
  }
  return { policyRevision: value.policyRevision, refs }
}

export function boundedRecallQuery(
  parts: Array<string | null | undefined>,
): string {
  const unique = new Set<string>()
  const normalized: string[] = []
  for (const part of parts) {
    const value = String(part ?? "").replace(/\s+/g, " ").trim()
    if (!value || unique.has(value)) continue
    unique.add(value)
    normalized.push(value)
  }
  return normalized.join("\n").slice(0, MAX_RECALL_QUERY_LENGTH).trim()
}

function recallLimit(value: number | undefined): number {
  if (value === undefined) return DEFAULT_RECALL_LIMIT
  if (!Number.isSafeInteger(value)) return DEFAULT_RECALL_LIMIT
  return Math.max(1, Math.min(MAX_READER_RECALL_LIMIT, value))
}
