import { DesktopResearchError, fetchResearchOperation } from "./client"
import type { ResearchOperation } from "./contracts"

// Statuses the server still advances on its own; anything else is final for
// this request identity and needs either the result or an explicit new request.
const IN_PROGRESS_STATUSES: ReadonlySet<ResearchOperation["status"]> = new Set(["waiting", "running"])

export function operationInProgress(status: ResearchOperation["status"]): boolean {
  return IN_PROGRESS_STATUSES.has(status)
}

export type OperationPollingSchedule = {
  initialDelayMs: number
  maxDelayMs: number
  backoffFactor: number
  // Consecutive transient read failures tolerated before giving up.
  maxConsecutiveFailures: number
}

export const DEFAULT_OPERATION_POLLING: OperationPollingSchedule = {
  initialDelayMs: 2_000,
  maxDelayMs: 15_000,
  backoffFactor: 1.5,
  maxConsecutiveFailures: 5,
}

export type FollowResearchOperationInput = {
  requestId: string
  generation: string
  signal: AbortSignal
  // Status the caller already applied, so the first identical poll is not re-reported.
  knownStatus?: ResearchOperation["status"]
  // Read once right away instead of waiting the first interval (resuming a saved request).
  immediate?: boolean
  // Called whenever the projected status changes; return false to stop following.
  onUpdate: (operation: ResearchOperation) => boolean | Promise<boolean>
  schedule?: Partial<OperationPollingSchedule>
  fetchOperation?: typeof fetchResearchOperation
}

/**
 * Poll one operation by its request identity until it leaves the in-progress
 * states. Polling only reads the durable operation record, so it never starts
 * or repeats work; transient read failures back off and retry, and a
 * non-retryable error ends polling by rejecting.
 */
export async function followResearchOperation({
  requestId,
  generation,
  signal,
  knownStatus,
  immediate = false,
  onUpdate,
  schedule,
  fetchOperation = fetchResearchOperation,
}: FollowResearchOperationInput): Promise<ResearchOperation | null> {
  const timing = { ...DEFAULT_OPERATION_POLLING, ...schedule }
  let delay = immediate ? 0 : timing.initialDelayMs
  let failures = 0
  let lastStatus: ResearchOperation["status"] | null = knownStatus ?? null
  let latest: ResearchOperation | null = null
  while (true) {
    await abortableDelay(delay, signal)
    try {
      latest = await fetchOperation(requestId, generation, signal)
      failures = 0
    } catch (cause) {
      if (signal.aborted || !transientReadFailure(cause)) throw cause
      failures += 1
      if (failures >= timing.maxConsecutiveFailures) throw cause
      delay = nextDelay(delay, timing)
      continue
    }
    if (latest.status !== lastStatus) {
      lastStatus = latest.status
      const keepFollowing = await onUpdate(latest)
      if (!keepFollowing || signal.aborted) return latest
    }
    if (!operationInProgress(latest.status)) return latest
    delay = nextDelay(delay, timing)
  }
}

function nextDelay(current: number, timing: OperationPollingSchedule): number {
  const grown = Math.max(timing.initialDelayMs, Math.round(current * timing.backoffFactor))
  return Math.min(timing.maxDelayMs, grown)
}

function transientReadFailure(cause: unknown): boolean {
  if (cause instanceof DesktopResearchError) {
    const status = cause.status ?? 0
    return status === 0 || status === 429 || status >= 500
  }
  // fetch rejects with TypeError when the network is briefly unavailable.
  return cause instanceof TypeError
}

function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(abortReason(signal))
      return
    }
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort)
      resolve()
    }, milliseconds)
    const onAbort = () => {
      clearTimeout(timer)
      reject(abortReason(signal))
    }
    signal.addEventListener("abort", onAbort, { once: true })
  })
}

function abortReason(signal: AbortSignal): unknown {
  return signal.reason ?? new DOMException("The operation was aborted.", "AbortError")
}
