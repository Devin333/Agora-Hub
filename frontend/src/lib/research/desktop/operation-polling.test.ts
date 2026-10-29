import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { DesktopResearchError } from "./client"
import type { ResearchOperation } from "./contracts"
import { followResearchOperation, operationInProgress } from "./operation-polling"

const generation = "gen_0123456789abcdef0123456789abcdef"

function operation(status: ResearchOperation["status"]): ResearchOperation {
  return {
    requestId: "request-analysis-1",
    generation,
    fingerprint: `sha256:${"b".repeat(64)}`,
    kind: "analyze",
    status,
    recoverable: true,
    automaticRetryAllowed: false,
  }
}

const schedule = { initialDelayMs: 100, maxDelayMs: 300, backoffFactor: 2, maxConsecutiveFailures: 3 }

describe("followResearchOperation", () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it("reports each status change once and stops at the terminal status", async () => {
    const fetchOperation = vi.fn()
      .mockResolvedValueOnce(operation("waiting"))
      .mockResolvedValueOnce(operation("running"))
      .mockResolvedValueOnce(operation("running"))
      .mockResolvedValueOnce(operation("passed"))
    const seen: string[] = []

    const followed = followResearchOperation({
      requestId: "request-analysis-1",
      generation,
      signal: new AbortController().signal,
      knownStatus: "waiting",
      schedule,
      fetchOperation,
      onUpdate: next => {
        seen.push(next.status)
        return true
      },
    })
    await vi.advanceTimersByTimeAsync(100 + 200 + 300 + 300)

    await expect(followed).resolves.toMatchObject({ status: "passed" })
    expect(seen).toEqual(["running", "passed"])
    expect(fetchOperation).toHaveBeenCalledTimes(4)
  })

  it("backs off between reads up to the maximum interval", async () => {
    const fetchOperation = vi.fn().mockResolvedValue(operation("running"))
    const controller = new AbortController()
    const followed = followResearchOperation({
      requestId: "request-analysis-1",
      generation,
      signal: controller.signal,
      schedule,
      fetchOperation,
      onUpdate: () => true,
    })
    followed.catch(() => undefined)

    await vi.advanceTimersByTimeAsync(99)
    expect(fetchOperation).toHaveBeenCalledTimes(0)
    await vi.advanceTimersByTimeAsync(1)
    expect(fetchOperation).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(200)
    expect(fetchOperation).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(300)
    expect(fetchOperation).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(300)
    expect(fetchOperation).toHaveBeenCalledTimes(4)

    controller.abort("user")
    await expect(followed).rejects.toBe("user")
  })

  it("reads immediately when resuming a saved request", async () => {
    const fetchOperation = vi.fn().mockResolvedValue(operation("passed"))
    const followed = followResearchOperation({
      requestId: "request-analysis-1",
      generation,
      signal: new AbortController().signal,
      immediate: true,
      schedule,
      fetchOperation,
      onUpdate: () => true,
    })
    await vi.advanceTimersByTimeAsync(0)

    await expect(followed).resolves.toMatchObject({ status: "passed" })
    expect(fetchOperation).toHaveBeenCalledTimes(1)
  })

  it("stops when the caller declines a receipt", async () => {
    const fetchOperation = vi.fn().mockResolvedValue(operation("running"))
    const followed = followResearchOperation({
      requestId: "request-analysis-1",
      generation,
      signal: new AbortController().signal,
      schedule,
      fetchOperation,
      onUpdate: () => false,
    })
    await vi.advanceTimersByTimeAsync(100)

    await expect(followed).resolves.toMatchObject({ status: "running" })
    await vi.advanceTimersByTimeAsync(1_000)
    expect(fetchOperation).toHaveBeenCalledTimes(1)
  })

  it("retries transient read failures and gives up after the limit", async () => {
    const unavailable = new DesktopResearchError("research_service_unavailable", "unavailable", 503)
    const fetchOperation = vi.fn()
      .mockRejectedValueOnce(unavailable)
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(operation("passed"))
    const recovered = followResearchOperation({
      requestId: "request-analysis-1",
      generation,
      signal: new AbortController().signal,
      schedule,
      fetchOperation,
      onUpdate: () => true,
    })
    await vi.advanceTimersByTimeAsync(100 + 200 + 300)
    await expect(recovered).resolves.toMatchObject({ status: "passed" })

    const failing = vi.fn().mockRejectedValue(unavailable)
    const exhausted = followResearchOperation({
      requestId: "request-analysis-1",
      generation,
      signal: new AbortController().signal,
      schedule,
      fetchOperation: failing,
      onUpdate: () => true,
    })
    exhausted.catch(() => undefined)
    await vi.advanceTimersByTimeAsync(100 + 200 + 300)
    await expect(exhausted).rejects.toBe(unavailable)
    expect(failing).toHaveBeenCalledTimes(3)
  })

  it("does not retry a request the service rejects", async () => {
    const missing = new DesktopResearchError("desktop_request_not_found", "missing", 404)
    const fetchOperation = vi.fn().mockRejectedValue(missing)
    const followed = followResearchOperation({
      requestId: "request-analysis-1",
      generation,
      signal: new AbortController().signal,
      schedule,
      fetchOperation,
      onUpdate: () => true,
    })
    followed.catch(() => undefined)
    await vi.advanceTimersByTimeAsync(100)

    await expect(followed).rejects.toBe(missing)
    expect(fetchOperation).toHaveBeenCalledTimes(1)
  })
})

describe("operationInProgress", () => {
  it("treats only waiting and running as advancing on the server", () => {
    expect(operationInProgress("waiting")).toBe(true)
    expect(operationInProgress("running")).toBe(true)
    for (const status of ["passed", "rejected", "failed", "needs_action", "interrupted", "unknown"] as const) {
      expect(operationInProgress(status)).toBe(false)
    }
  })
})
