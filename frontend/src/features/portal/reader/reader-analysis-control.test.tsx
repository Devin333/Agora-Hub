import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"
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
vi.mock("@/lib/research/desktop/client", () => ({
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
    mocks.recoverable = [{
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
      status: "unknown",
      paperId: paper.id,
      sourceRevision: "revision-1",
      createdAt: 1,
      updatedAt: 1,
    }]
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
