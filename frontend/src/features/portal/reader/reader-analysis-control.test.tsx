import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { Paper } from "@/lib/papers/types"
import { ReaderAnalysisControl } from "./reader-analysis-control"

const memoryId = `pmem_${"1".repeat(32)}`
const generation = "gen_0123456789abcdef0123456789abcdef"
const fingerprint = `sha256:${"b".repeat(64)}`
const sourceFingerprint = `sha256:${"a".repeat(64)}`
const selection = {
  policyRevision: 2,
  refs: [{ memoryId, revision: 3 }],
}

const mocks = vi.hoisted(() => ({
  fetchDesktopIdentity: vi.fn(),
  fetchResearchOperation: vi.fn(),
  submitResearchAnalyze: vi.fn(),
  previewPersonalMemoryTask: vi.fn(),
  prepareResearchOperation: vi.fn(),
  updateResearchOperation: vi.fn(),
  recoverable: [] as Array<Record<string, unknown>>,
}))

vi.mock("@/lib/research/use-research-history", () => ({
  useOwnedResearchWorkspace: () => ({ owner: "alice", ready: true, workspace: {} }),
}))
vi.mock("@/lib/research/desktop/client", async importOriginal => ({
  ...(await importOriginal<typeof import("@/lib/research/desktop/client")>()),
  fetchDesktopIdentity: mocks.fetchDesktopIdentity,
  fetchResearchOperation: mocks.fetchResearchOperation,
  submitResearchAnalyze: mocks.submitResearchAnalyze,
  previewPersonalMemoryTask: mocks.previewPersonalMemoryTask,
}))
vi.mock("@/lib/research/pending-operations", () => ({
  operationReceiptMatches: (
    actual: { requestId: string; generation: string; kind: string; fingerprint: string },
    expected: { requestId: string; generation: string; kind: string; fingerprint: string },
  ) => actual.requestId === expected.requestId &&
    actual.generation === expected.generation &&
    actual.kind === expected.kind &&
    actual.fingerprint === expected.fingerprint,
  prepareResearchOperation: mocks.prepareResearchOperation,
  recoverableResearchOperations: () => mocks.recoverable,
  updateResearchOperation: mocks.updateResearchOperation,
}))

const paper: Paper = {
  id: "2601.01234",
  slug: "2601.01234",
  title: "Bounded retrieval paper",
  abstractSnippet: "A methods-focused retrieval study.",
  authors: [],
  publishedAt: "2026-10-12",
  tags: ["retrieval"],
  taskRefs: [],
  methodRefs: [],
  isPublished: false,
}

function passedOperation() {
  return {
    requestId: "request-analysis-1",
    generation,
    fingerprint,
    kind: "analyze" as const,
    status: "passed" as const,
    updatedAt: "2026-10-14T00:00:00Z",
    paperId: paper.id,
    sourceRevision: "revision-1",
    runId: "run-analysis-1",
    recoverable: true,
    automaticRetryAllowed: false as const,
    result: {
      runId: "run-analysis-1",
      paperId: paper.id,
      sourceRevision: "revision-1",
      status: "succeeded",
      accepted: true,
      qualityPassed: true,
      eligibleForAcceptedExport: true,
      artifactStatus: "valid",
      usedPersonalMemory: [{ memoryId, revision: 3 }],
    },
  }
}

function inProgressOperation(status: "waiting" | "running") {
  const operation: Partial<ReturnType<typeof passedOperation>> = passedOperation()
  delete operation.result
  return { ...operation, status, runId: "run-analysis-1" }
}

function recoverableAnalysis(status: string) {
  return {
    requestId: "request-analysis-1",
    generation,
    fingerprint,
    kind: "analyze",
    semantic: {
      kind: "analyze",
      paperId: paper.id,
      sourceRevision: "revision-1",
      sourceFingerprint,
      sourceUrl: "https://example.test/paper",
      pdfUrl: "https://example.test/paper.pdf",
      objective: "Analyze paper 2601.01234",
      constraints: [],
      personalMemory: selection,
    },
    status,
    paperId: paper.id,
    sourceRevision: "revision-1",
    createdAt: 1,
    updatedAt: 1,
  }
}

function renderControl(onRun = vi.fn()) {
  return {
    onRun,
    ...render(
      <ReaderAnalysisControl
        paper={paper}
        sourceRevision="revision-1"
        sourceFingerprint={sourceFingerprint}
        sourceUrl="https://example.test/paper"
        pdfUrl="https://example.test/paper.pdf"
        enabled
        onRun={onRun}
        personalMemory={{ enabled: true, limit: 5 }}
      />,
    ),
  }
}

describe("ReaderAnalysisControl personal memory", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.recoverable = []
    mocks.fetchDesktopIdentity.mockResolvedValue({ generation })
    mocks.previewPersonalMemoryTask.mockResolvedValue({
      policy: {
        policy: {
          allow_remote_processing: true,
          revision: 2,
          created_at: "2026-10-14T00:00:00+00:00",
          updated_at: "2026-10-14T00:00:00+00:00",
        },
      },
      recall: null,
      reason: "matched",
      selection,
    })
    mocks.prepareResearchOperation.mockResolvedValue({
      requestId: "request-analysis-1",
      generation,
      fingerprint,
      kind: "analyze",
      semantic: {},
      status: "prepared",
      createdAt: 1,
      updatedAt: 1,
    })
    mocks.submitResearchAnalyze.mockResolvedValue(passedOperation())
  })

  it("recalls before canonical analysis preparation and submits the frozen refs", async () => {
    const { onRun } = renderControl()
    fireEvent.click(screen.getByRole("button", { name: "开始真实分析" }))

    await waitFor(() => expect(onRun).toHaveBeenCalledWith("run-analysis-1"))
    expect(mocks.previewPersonalMemoryTask).toHaveBeenCalledWith(
      expect.objectContaining({ useMemory: true, limit: 5 }),
      expect.any(AbortSignal),
    )
    const prepared = mocks.prepareResearchOperation.mock.calls[0][0]
    expect(prepared.semantic.personalMemory).toEqual(selection)
    expect(prepared.semantic.objective).toBe("Analyze paper 2601.01234")
    expect(mocks.submitResearchAnalyze.mock.calls[0][0].personalMemory).toEqual(selection)
  })

  it("refuses a passed receipt whose deterministic quality contract is not accepted", async () => {
    const invalid = passedOperation()
    invalid.result.qualityPassed = false
    mocks.submitResearchAnalyze.mockResolvedValue(invalid)
    const { onRun } = renderControl()

    fireEvent.click(screen.getByRole("button", { name: "开始真实分析" }))

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("确定性质量门回执无效"))
    expect(onRun).not.toHaveBeenCalled()
    expect(mocks.updateResearchOperation).toHaveBeenCalledWith(
      "request-analysis-1",
      { status: "unknown" },
    )
  })

  it("refuses a passed receipt whose run identity differs from its result", async () => {
    const invalid = passedOperation()
    invalid.result.runId = "run-other"
    mocks.submitResearchAnalyze.mockResolvedValue(invalid)
    const { onRun } = renderControl()

    fireEvent.click(screen.getByRole("button", { name: "开始真实分析" }))

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("不能作为已接受报告"))
    expect(onRun).not.toHaveBeenCalled()
  })

  it("recovers with the original memory snapshot without recalling or resubmitting", async () => {
    mocks.recoverable = [recoverableAnalysis("unknown")]
    mocks.fetchResearchOperation.mockResolvedValue(passedOperation())
    const { onRun } = renderControl()

    fireEvent.click(await screen.findByRole("button", { name: /检查已有请求/ }))
    await waitFor(() => expect(onRun).toHaveBeenCalledWith("run-analysis-1"))
    expect(mocks.fetchResearchOperation).toHaveBeenCalledWith(
      "request-analysis-1",
      generation,
      expect.any(AbortSignal),
    )
    expect(mocks.previewPersonalMemoryTask).not.toHaveBeenCalled()
    expect(mocks.submitResearchAnalyze).not.toHaveBeenCalled()
  })
})

describe("ReaderAnalysisControl background analysis", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mocks.recoverable = []
    mocks.fetchDesktopIdentity.mockResolvedValue({ generation })
    mocks.previewPersonalMemoryTask.mockResolvedValue({
      policy: {
        policy: {
          allow_remote_processing: true,
          revision: 2,
          created_at: "2026-10-14T00:00:00+00:00",
          updated_at: "2026-10-14T00:00:00+00:00",
        },
      },
      recall: null,
      reason: "matched",
      selection,
    })
    mocks.prepareResearchOperation.mockResolvedValue({
      requestId: "request-analysis-1",
      generation,
      fingerprint,
      kind: "analyze",
      semantic: {},
      status: "prepared",
      createdAt: 1,
      updatedAt: 1,
    })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it("follows a waiting submission until the background run passes", async () => {
    mocks.submitResearchAnalyze.mockResolvedValue(inProgressOperation("waiting"))
    mocks.fetchResearchOperation
      .mockResolvedValueOnce(inProgressOperation("running"))
      .mockResolvedValueOnce(inProgressOperation("running"))
      .mockResolvedValueOnce(passedOperation())
    const { onRun } = renderControl()

    fireEvent.click(screen.getByRole("button", { name: "开始真实分析" }))
    expect(await screen.findByText("分析已排队，等待后台开始运行")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /正在自动刷新/ })).toBeDisabled()

    await vi.advanceTimersByTimeAsync(2_000)
    expect(await screen.findByText("分析正在后台运行，完成后自动显示结果")).toBeInTheDocument()
    expect(onRun).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(3_000)
    await vi.advanceTimersByTimeAsync(4_500)
    await waitFor(() => expect(onRun).toHaveBeenCalledWith("run-analysis-1"))
    expect(mocks.submitResearchAnalyze).toHaveBeenCalledTimes(1)
    expect(mocks.fetchResearchOperation).toHaveBeenCalledTimes(3)
    expect(mocks.updateResearchOperation).toHaveBeenLastCalledWith(
      "request-analysis-1",
      { status: "passed", runId: "run-analysis-1" },
    )
  })

  it("resumes following a saved running request after the reader remounts", async () => {
    mocks.recoverable = [recoverableAnalysis("running")]
    mocks.fetchResearchOperation.mockResolvedValue(passedOperation())
    const { onRun } = renderControl()

    await waitFor(() => expect(onRun).toHaveBeenCalledWith("run-analysis-1"))
    expect(mocks.fetchResearchOperation).toHaveBeenCalledWith(
      "request-analysis-1",
      generation,
      expect.any(AbortSignal),
    )
    expect(mocks.submitResearchAnalyze).not.toHaveBeenCalled()
  })

  it("stops refreshing on request without marking the background run unknown", async () => {
    mocks.submitResearchAnalyze.mockResolvedValue(inProgressOperation("waiting"))
    renderControl()

    fireEvent.click(screen.getByRole("button", { name: "开始真实分析" }))
    fireEvent.click(await screen.findByRole("button", { name: "停止自动刷新分析状态" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("分析仍在后台运行")
    expect(screen.getByText("分析已排队，等待后台开始运行")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /检查已有请求/ })).toBeEnabled()
    expect(mocks.updateResearchOperation).not.toHaveBeenCalledWith(
      "request-analysis-1",
      { status: "unknown" },
    )
    await vi.advanceTimersByTimeAsync(10_000)
    expect(mocks.fetchResearchOperation).not.toHaveBeenCalled()
  })

  it("marks a saved request the service never admitted as failed so a new identity can retry", async () => {
    const { DesktopResearchError } = await import("@/lib/research/desktop/client")
    mocks.recoverable = [recoverableAnalysis("waiting")]
    mocks.fetchResearchOperation.mockRejectedValue(
      new DesktopResearchError("desktop_request_not_found", "Desktop request was not found", 404),
    )
    renderControl()

    expect(await screen.findByRole("alert")).toHaveTextContent("服务端没有这条分析请求")
    expect(screen.getByRole("button", { name: "使用新请求身份重试" })).toBeInTheDocument()
    expect(mocks.updateResearchOperation).toHaveBeenCalledWith(
      "request-analysis-1",
      { status: "failed", errorCode: "desktop_request_not_found" },
    )
  })
})
