import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"
import type { Paper } from "@/lib/papers/types"
import { ReaderPreparationControl, validPreparedSourceResult } from "./reader-preparation-control"

const mocks = vi.hoisted(() => ({
  fetchDesktopIdentity: vi.fn(),
  fetchResearchOperation: vi.fn(),
  submitResearchPrepare: vi.fn(),
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
  submitResearchPrepare: mocks.submitResearchPrepare,
}))
vi.mock("@/lib/research/pending-operations", () => ({
  operationReceiptMatches: (
    operation: { requestId: string; generation: string; kind: string; fingerprint: string },
    expected: { requestId: string; generation: string; kind: string; fingerprint: string },
  ) =>
    operation.requestId === expected.requestId &&
    operation.generation === expected.generation &&
    operation.kind === expected.kind &&
    operation.fingerprint === expected.fingerprint,
  prepareResearchOperation: mocks.prepareResearchOperation,
  recoverableResearchOperations: () => mocks.recoverable,
  updateResearchOperation: mocks.updateResearchOperation,
}))

const generation = "gen_0123456789abcdef0123456789abcdef"
const fingerprint = `sha256:${"a".repeat(64)}`
const paper: Paper = {
  id: "2601.01234",
  slug: "2601.01234",
  title: "Public paper",
  abstractSnippet: "",
  authors: [],
  publishedAt: "2026-10-12",
  tags: [],
  taskRefs: [],
  methodRefs: [],
  isPublished: false,
}

function operation(status: "passed" | "running" = "passed") {
  return {
    requestId: "request-prepare-1",
    generation,
    fingerprint,
    kind: "prepare" as const,
    status,
    paperId: paper.id,
    recoverable: true,
    automaticRetryAllowed: false as const,
    ...(status === "passed"
      ? {
          result: {
            status: "ready",
            paperId: paper.id,
            sourceRevision: "revision-1",
            sourceFingerprint: fingerprint,
          },
        }
      : {}),
  }
}

describe("ReaderPreparationControl", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.recoverable = []
    mocks.fetchDesktopIdentity.mockResolvedValue({
      owner: { userId: "alice", tenantId: null, memoryNamespace: "research:user:alice" },
      generation,
      requestContract: {
        fingerprint: "sha256-canonical-json-v1",
        requestId: "uuid",
        notFoundRetry: "query by request identity",
      },
    })
    mocks.prepareResearchOperation.mockResolvedValue({
      requestId: "request-prepare-1",
      generation,
      fingerprint,
      kind: "prepare",
      status: "prepared",
      paperId: paper.id,
      semantic: {
        kind: "prepare",
        paperId: paper.id,
        sourceUrl: "https://arxiv.org/abs/2601.01234",
        sourceType: null,
      },
      createdAt: 1,
      updatedAt: 1,
    })
  })

  it("persists request identity before send and queries the same request after a lost first receipt", async () => {
    const onReady = vi.fn()
    mocks.submitResearchPrepare.mockRejectedValueOnce(new Error("connection lost"))
    mocks.fetchResearchOperation.mockResolvedValueOnce(operation("passed"))

    render(
      <ReaderPreparationControl
        paper={paper}
        sourceUrl="https://arxiv.org/abs/2601.01234"
        enabled
        onReady={onReady}
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: "准备正文" }))
    await screen.findByText("请求结果未知，需要按原身份查询")
    expect(mocks.prepareResearchOperation).toHaveBeenCalledTimes(1)
    expect(mocks.submitResearchPrepare).toHaveBeenCalledTimes(1)
    expect(
      mocks.prepareResearchOperation.mock.invocationCallOrder[0],
    ).toBeLessThan(mocks.submitResearchPrepare.mock.invocationCallOrder[0])

    fireEvent.click(screen.getByRole("button", { name: "检查已有请求" }))
    await waitFor(() => expect(onReady).toHaveBeenCalledTimes(1))
    expect(mocks.fetchResearchOperation).toHaveBeenCalledWith(
      "request-prepare-1",
      generation,
      expect.any(AbortSignal),
    )
    expect(mocks.submitResearchPrepare).toHaveBeenCalledTimes(1)
  })

  it("restores an existing unknown prepare request without creating a new identity", async () => {
    mocks.recoverable = [{
      requestId: "request-prepare-1",
      generation,
      fingerprint,
      kind: "prepare",
      status: "unknown",
      paperId: paper.id,
      semantic: {
        kind: "prepare",
        paperId: paper.id,
        sourceUrl: "https://arxiv.org/abs/2601.01234",
        sourceType: null,
      },
      createdAt: 1,
      updatedAt: 2,
    }]
    mocks.fetchResearchOperation.mockResolvedValueOnce(operation("running"))

    render(
      <ReaderPreparationControl
        paper={paper}
        sourceUrl="https://arxiv.org/abs/2601.01234"
        enabled
        onReady={() => undefined}
      />,
    )

    await screen.findByText("正在解析真实论文来源")
    expect(mocks.fetchResearchOperation).toHaveBeenCalledWith(
      "request-prepare-1",
      generation,
      expect.any(AbortSignal),
    )
    expect(mocks.prepareResearchOperation).not.toHaveBeenCalled()
    expect(mocks.submitResearchPrepare).not.toHaveBeenCalled()
  })

  it("does not accept a successful receipt with a mismatched durable identity", async () => {
    const onReady = vi.fn()
    mocks.submitResearchPrepare.mockResolvedValueOnce({
      ...operation("passed"),
      fingerprint: `sha256:${"b".repeat(64)}`,
    })

    render(
      <ReaderPreparationControl
        paper={paper}
        sourceUrl="https://arxiv.org/abs/2601.01234"
        enabled
        onReady={onReady}
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: "准备正文" }))
    await screen.findByText(/回执与已保存的请求身份不一致/)
    expect(onReady).not.toHaveBeenCalled()
    expect(mocks.updateResearchOperation).toHaveBeenCalledWith(
      "request-prepare-1",
      { status: "unknown" },
    )
  })

  it("requires the exact completed source identity before refreshing the reader", () => {
    expect(validPreparedSourceResult(operation("passed"), paper.id)).toBe(true)
    expect(validPreparedSourceResult({
      ...operation("passed"),
      result: { status: "ready", paperId: paper.id },
    }, paper.id)).toBe(false)
    expect(validPreparedSourceResult(operation("passed"), "other-paper")).toBe(false)
  })
})
