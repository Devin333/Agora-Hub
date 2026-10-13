import { useCallback, useState } from "react"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"
import type { ReaderSelectionAskRequest } from "@/components/papers/open-reader/open-reader-types"
import type { Paper } from "@/lib/papers/types"
import type { useReaderNotebook } from "./use-reader-notebook"
import { ReaderAssistant } from "./reader-assistant"

const mocks = vi.hoisted(() => ({
  fetchDesktopIdentity: vi.fn(),
  fetchResearchOperation: vi.fn(),
  submitResearchAsk: vi.fn(),
  prepareResearchOperation: vi.fn(),
  updateResearchOperation: vi.fn(),
  owner: "alice" as string | null,
}))

vi.mock("@/lib/deployment-profile", () => ({ isLocalDesktopProfile: () => true }))
vi.mock("@/lib/research/use-research-history", () => ({
  useOwnedResearchWorkspace: () => ({ owner: mocks.owner, ready: true, workspace: {} }),
}))
vi.mock("@/lib/research/desktop/client", () => ({
  fetchDesktopIdentity: mocks.fetchDesktopIdentity,
  fetchResearchOperation: mocks.fetchResearchOperation,
  submitResearchAsk: mocks.submitResearchAsk,
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
  recoverableResearchOperations: () => [],
  updateResearchOperation: mocks.updateResearchOperation,
}))

type Notebook = ReturnType<typeof useReaderNotebook>

const paper: Paper = {
  id: "2601.01234",
  slug: "2601.01234",
  title: "Grounded paper",
  abstractSnippet: "",
  authors: [],
  publishedAt: "2026-10-12",
  tags: [],
  taskRefs: [],
  methodRefs: [],
  isPublished: false,
}

const selectionRequest: ReaderSelectionAskRequest = {
  requestKey: "selection-request-1",
  selectionId: "selection-1",
  mode: "explain",
  question: "请解释这段原文",
  selection: { sectionId: "methods", page: 3, selectedText: "关键原文" },
  owner: "alice",
  sourceRevision: "revision-1",
}

function Harness({
  onSelectionAnswered,
  selection = selectionRequest,
}: {
  onSelectionAnswered?: ReturnType<typeof vi.fn>
  selection?: ReaderSelectionAskRequest
}) {
  const [question, setQuestion] = useState("")
  const [, forceRender] = useState(0)
  const update = useCallback((patch: Parameters<Notebook["update"]>[0]) => {
    if (patch.question !== undefined) setQuestion(patch.question)
  }, [])
  const notebook = {
    paperId: paper.id,
    sourceRevision: "revision-1",
    note: "",
    question,
    fontSize: 18 as const,
    sectionId: "",
    pdfPage: 1,
    updatedAt: 1,
    update,
    ready: true,
    storageError: false,
    resumeSection: "",
    revisionChanged: false,
    legacyAvailable: false,
    importLegacy: () => false,
    saveStatus: "synced" as const,
  } satisfies Notebook

  return (
    <>
      <button type="button" onClick={() => forceRender(value => value + 1)}>父组件重渲染</button>
      <ReaderAssistant
        paper={paper}
        notebook={notebook}
        sectionIds={new Set(["methods"])}
        onNavigateCitation={() => undefined}
        researchQuestion=""
        sourceRevision="revision-1"
        sourceFingerprint={`sha256:${"a".repeat(64)}`}
        runId="run-1"
        selectionRequest={selection}
        onSelectionAnswered={onSelectionAnswered}
      />
    </>
  )
}

describe("ReaderAssistant grounded selection prefill", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.owner = "alice"
    mocks.fetchDesktopIdentity.mockResolvedValue({
      owner: { userId: "alice", tenantId: null, memoryNamespace: "research:user:alice" },
      generation: "gen_0123456789abcdef0123456789abcdef",
      requestContract: {
        fingerprint: "sha256-canonical-json-v1",
        requestId: "uuid",
        notFoundRetry: "query by request identity",
      },
    })
    mocks.prepareResearchOperation.mockResolvedValue({
      requestId: "request-selection-1",
      generation: "gen_0123456789abcdef0123456789abcdef",
      fingerprint: `sha256:${"b".repeat(64)}`,
      kind: "ask",
    })
  })

  it("does not abort a sent request or clear its answer when the parent returns a new notebook object", async () => {
    let resolveSubmission: ((value: unknown) => void) | undefined
    let submissionSignal: AbortSignal | undefined
    mocks.submitResearchAsk.mockImplementation((_body: unknown, signal: AbortSignal) => {
      submissionSignal = signal
      return new Promise(resolve => { resolveSubmission = resolve })
    })

    render(<Harness />)
    const question = await screen.findByPlaceholderText("例如：这个方法适合什么场景？")
    await waitFor(() => expect(question).toHaveValue("请解释这段原文"))
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }))
    await screen.findByText("正在核对真实运行状态…")

    fireEvent.click(screen.getByRole("button", { name: "父组件重渲染" }))
    expect(submissionSignal?.aborted).toBe(false)
    expect(screen.getByText("正在核对真实运行状态…")).toBeInTheDocument()

    resolveSubmission?.({
      requestId: "request-selection-1",
      generation: "gen_0123456789abcdef0123456789abcdef",
      fingerprint: `sha256:${"b".repeat(64)}`,
      kind: "ask",
      status: "passed",
      runId: "run-1",
      paperId: paper.id,
      sourceRevision: "revision-1",
      recoverable: true,
      automaticRetryAllowed: false,
      result: {
        runId: "run-1",
        paperId: paper.id,
        sourceRevision: "revision-1",
        answer: "这是绑定所选原文的解释。",
        citations: [{
          id: "citation-1",
          label: "Methods",
          sectionId: "methods",
          page: 3,
          valid: true,
        }],
        confidence: 0.9,
      },
    })
    await screen.findByText("这是绑定所选原文的解释。")
    fireEvent.click(screen.getByRole("button", { name: "父组件重渲染" }))
    expect(screen.getByText("这是绑定所选原文的解释。")).toBeInTheDocument()
  })

  it("aborts and discards the old response when a new selection is requested", async () => {
    let resolveSubmission: ((value: unknown) => void) | undefined
    let submissionSignal: AbortSignal | undefined
    mocks.submitResearchAsk.mockImplementation((_body: unknown, signal: AbortSignal) => {
      submissionSignal = signal
      return new Promise(resolve => { resolveSubmission = resolve })
    })
    const replacement: ReaderSelectionAskRequest = {
      ...selectionRequest,
      requestKey: "selection-request-2",
      selectionId: "selection-2",
      question: "请举例说明这段原文",
    }
    const onSelectionAnswered = vi.fn()
    const view = render(<Harness onSelectionAnswered={onSelectionAnswered} />)
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("请解释这段原文"))
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }))
    await screen.findByText("正在核对真实运行状态…")

    view.rerender(
      <Harness selection={replacement} onSelectionAnswered={onSelectionAnswered} />,
    )
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("请举例说明这段原文"))
    expect(submissionSignal?.aborted).toBe(true)
    resolveSubmission?.({
      requestId: "request-selection-1",
      generation: "gen_0123456789abcdef0123456789abcdef",
      fingerprint: `sha256:${"b".repeat(64)}`,
      kind: "ask",
      status: "passed",
      runId: "run-1",
      paperId: paper.id,
      sourceRevision: "revision-1",
      recoverable: true,
      automaticRetryAllowed: false,
      result: {
        runId: "run-1",
        paperId: paper.id,
        sourceRevision: "revision-1",
        answer: "旧选择回答",
        citations: [],
        confidence: 0.8,
      },
    })
    await waitFor(() => expect(screen.queryByText("旧选择回答")).not.toBeInTheDocument())
    expect(onSelectionAnswered).not.toHaveBeenCalled()
  })

  it("discards an old owner response after an account switch on the same page", async () => {
    let resolveSubmission: ((value: unknown) => void) | undefined
    mocks.submitResearchAsk.mockImplementation(() => new Promise(resolve => {
      resolveSubmission = resolve
    }))
    const onSelectionAnswered = vi.fn()
    const view = render(<Harness onSelectionAnswered={onSelectionAnswered} />)
    const question = await screen.findByPlaceholderText("例如：这个方法适合什么场景？")
    await waitFor(() => expect(question).toHaveValue("请解释这段原文"))
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }))
    await screen.findByText("正在核对真实运行状态…")

    mocks.owner = "bob"
    view.rerender(<Harness onSelectionAnswered={onSelectionAnswered} />)
    resolveSubmission?.({
      requestId: "request-selection-1",
      generation: "gen_0123456789abcdef0123456789abcdef",
      fingerprint: `sha256:${"b".repeat(64)}`,
      kind: "ask",
      status: "passed",
      runId: "run-1",
      paperId: paper.id,
      sourceRevision: "revision-1",
      recoverable: true,
      automaticRetryAllowed: false,
      result: {
        runId: "run-1",
        paperId: paper.id,
        sourceRevision: "revision-1",
        answer: "旧账号回答",
        citations: [],
        confidence: 0.8,
      },
    })

    await waitFor(() => expect(screen.queryByText("旧账号回答")).not.toBeInTheDocument())
    expect(onSelectionAnswered).not.toHaveBeenCalled()
  })
})
