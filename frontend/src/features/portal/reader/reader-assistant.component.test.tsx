import { useCallback, useState } from "react"
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"
import type { ReaderSelectionAskRequest } from "@/components/papers/open-reader/open-reader-types"
import type { Paper, PaperReaderAnswer } from "@/lib/papers/types"
import type { useReaderNotebook } from "./use-reader-notebook"
import { ReaderAssistant, type ReaderAssistantProps } from "./reader-assistant"

const mocks = vi.hoisted(() => ({
  hostedProfile: true,
  askPaper: vi.fn(),
  fetchDesktopIdentity: vi.fn(),
  fetchResearchOperation: vi.fn(),
  submitResearchAsk: vi.fn(),
  previewPersonalMemoryTask: vi.fn(),
  prepareResearchOperation: vi.fn(),
  updateResearchOperation: vi.fn(),
  recoverable: [] as Array<Record<string, unknown>>,
  owner: "alice" as string | null,
}))

vi.mock("@/lib/deployment-profile", () => ({ isHostedProfile: () => mocks.hostedProfile }))
vi.mock("@/lib/papers/api", () => ({ askPaper: mocks.askPaper }))
vi.mock("@/lib/research/use-research-history", () => ({
  useOwnedResearchWorkspace: () => ({ owner: mocks.owner, ready: true, workspace: {} }),
}))
vi.mock("@/lib/research/desktop/client", () => ({
  fetchDesktopIdentity: mocks.fetchDesktopIdentity,
  fetchResearchOperation: mocks.fetchResearchOperation,
  submitResearchAsk: mocks.submitResearchAsk,
  previewPersonalMemoryTask: mocks.previewPersonalMemoryTask,
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

const desktopTrace = {
  transcriptId: "rag-transcript://paper_rag_session:0123456789abcdef/1a2b3c4d",
  answerStatus: "answered" as const,
  evidenceRefs: ["evidence-1"],
}

const groundedCitation = {
  id: "citation-1",
  label: "Methods",
  evidenceId: "evidence-1",
  sectionId: "methods",
  page: 3,
  valid: true,
}

function groundedAnswerResult(answer: string) {
  return {
    runId: "run-1",
    paperId: paper.id,
    sourceRevision: "revision-1",
    answer,
    citations: [groundedCitation],
    confidence: 1,
    ...desktopTrace,
  }
}

function groundedOperation(result: Record<string, unknown>) {
  return {
    requestId: "request-selection-1",
    generation: "gen_0123456789abcdef0123456789abcdef",
    fingerprint: `sha256:${"b".repeat(64)}`,
    kind: "ask",
    status: "passed",
    runId: "run-1",
    paperId: paper.id,
    sourceRevision: "revision-1",
    recoverable: false,
    automaticRetryAllowed: false,
    result,
  }
}

function Harness({
  onSelectionAnswered,
  selection = selectionRequest,
  usePersonalMemory = false,
  onTaskActivity,
  onUnsavedChanges,
  onExportNote,
  note = "",
  saveStatus = "synced",
}: {
  onSelectionAnswered?: ReaderAssistantProps["onSelectionAnswered"]
  selection?: ReaderSelectionAskRequest | null
  usePersonalMemory?: boolean
  onTaskActivity?: ReaderAssistantProps["onTaskActivity"]
  onUnsavedChanges?: ReaderAssistantProps["onUnsavedChanges"]
  onExportNote?: ReaderAssistantProps["onExportNote"]
  note?: string
  saveStatus?: Notebook["saveStatus"]
}) {
  const [question, setQuestion] = useState("")
  const [, forceRender] = useState(0)
  const update = useCallback((patch: Parameters<Notebook["update"]>[0]) => {
    if (patch.question !== undefined) setQuestion(patch.question)
  }, [])
  const notebook = {
    paperId: paper.id,
    sourceRevision: "revision-1",
    note,
    question,
    fontSize: 18 as const,
    sectionId: "",
    pdfPage: 1,
    updatedAt: 1,
    update,
    ready: true,
    storageError: false,
    noteOverLimit: note.length > 20000,
    resumeSection: "",
    revisionChanged: false,
    legacyAvailable: false,
    importLegacy: () => false,
    saveStatus,
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
        selectionRequest={selection ?? undefined}
        onSelectionAnswered={onSelectionAnswered}
        personalMemory={{ enabled: usePersonalMemory, limit: 5 }}
        onTaskActivity={onTaskActivity}
        onUnsavedChanges={onUnsavedChanges}
        onExportNote={onExportNote}
      />
    </>
  )
}

describe("ReaderAssistant grounded selection prefill", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.hostedProfile = true
    mocks.owner = "alice"
    mocks.recoverable = []
    mocks.fetchDesktopIdentity.mockResolvedValue({
      owner: { userId: "alice", tenantId: null, memoryNamespace: "research:user:alice" },
      generation: "gen_0123456789abcdef0123456789abcdef",
      requestContract: {
        fingerprint: "sha256-canonical-json-v1",
        requestId: "uuid",
        notFoundRetry: "query by request identity",
      },
    })
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
      reason: "no_match",
    })
    mocks.prepareResearchOperation.mockResolvedValue({
      requestId: "request-selection-1",
      generation: "gen_0123456789abcdef0123456789abcdef",
      fingerprint: `sha256:${"b".repeat(64)}`,
      kind: "ask",
    })
  })

  it("freezes bounded recalled refs into the canonical ask request and accepts only matching disclosure", async () => {
    const memoryId = `pmem_${"1".repeat(32)}`
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
      selection: {
        policyRevision: 2,
        refs: [{ memoryId, revision: 3 }],
      },
    })
    mocks.submitResearchAsk.mockResolvedValue({
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
        ...desktopTrace,
        runId: "run-1",
        paperId: paper.id,
        sourceRevision: "revision-1",
        answer: "使用有界检索。",
        citations: [groundedCitation],
        confidence: 0.8,
        usedPersonalMemory: [{ memoryId, revision: 3 }],
      },
    })

    const onTaskActivity = vi.fn<NonNullable<ReaderAssistantProps["onTaskActivity"]>>()
    render(<Harness usePersonalMemory onTaskActivity={onTaskActivity} />)
    const question = await screen.findByPlaceholderText("例如：这个方法适合什么场景？")
    await waitFor(() => expect(question).toHaveValue("请解释这段原文"))
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }))

    await screen.findByText("使用有界检索。")
    expect(mocks.previewPersonalMemoryTask).toHaveBeenCalledWith(
      expect.objectContaining({ useMemory: true, limit: 5 }),
      expect.any(AbortSignal),
    )
    const prepared = mocks.prepareResearchOperation.mock.calls[0][0]
    expect(prepared.semantic.personalMemory).toEqual({
      policyRevision: 2,
      refs: [{ memoryId, revision: 3 }],
    })
    const submitted = mocks.submitResearchAsk.mock.calls[0][0]
    expect(submitted.personalMemory).toEqual(prepared.semantic.personalMemory)
    expect(onTaskActivity).toHaveBeenLastCalledWith(expect.objectContaining({
      kind: "ask",
      status: "passed",
      usedPersonalMemory: [{ memoryId, revision: 3 }],
    }))
  })

  it("reports unsaved note state and delegates export through the optional bridge", async () => {
    const onUnsavedChanges = vi.fn<NonNullable<ReaderAssistantProps["onUnsavedChanges"]>>()
    const onExportNote = vi.fn<NonNullable<ReaderAssistantProps["onExportNote"]>>()
    render(
      <Harness
        selection={null}
        note="Methods note"
        saveStatus="loading"
        onUnsavedChanges={onUnsavedChanges}
        onExportNote={onExportNote}
      />,
    )

    await waitFor(() => expect(onUnsavedChanges).toHaveBeenCalledWith(true))
    fireEvent.click(screen.getByRole("button", { name: /阅读笔记/ }))
    fireEvent.click(screen.getByRole("button", { name: "导出 Markdown" }))
    expect(onExportNote).toHaveBeenCalledWith({
      filename: "2601-01234-notes.md",
      content: expect.stringContaining("Methods note"),
    })
  })

  it("recovers an ask with its original memory snapshot without recalling or resubmitting", async () => {
    const memoryId = `pmem_${"1".repeat(32)}`
    mocks.recoverable = [{
      requestId: "request-selection-1",
      generation: "gen_0123456789abcdef0123456789abcdef",
      fingerprint: `sha256:${"b".repeat(64)}`,
      kind: "ask",
      semantic: {
        kind: "ask",
        runId: "run-1",
        paperId: paper.id,
        sourceRevision: "revision-1",
        sourceFingerprint: `sha256:${"a".repeat(64)}`,
        question: "请解释这段原文",
        selection: selectionRequest.selection,
        locale: "zh",
        personalMemory: {
          policyRevision: 2,
          refs: [{ memoryId, revision: 3 }],
        },
      },
      status: "unknown",
      paperId: paper.id,
      sourceRevision: "revision-1",
      question: "请解释这段原文",
      createdAt: 1,
      updatedAt: 1,
    }]
    mocks.fetchResearchOperation.mockResolvedValue({
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
        ...desktopTrace,
        runId: "run-1",
        paperId: paper.id,
        sourceRevision: "revision-1",
        answer: "恢复原请求回答",
        citations: [groundedCitation],
        confidence: 0.8,
        usedPersonalMemory: [{ memoryId, revision: 3 }],
      },
    })

    render(<Harness usePersonalMemory selection={null} />)
    await screen.findByText("恢复原请求回答")
    const trace = screen.getByRole("region", { name: "回答执行追溯" })
    fireEvent.click(within(trace).getByText("回答执行追溯"))
    expect(within(trace).getByText(desktopTrace.transcriptId)).toBeVisible()
    expect(within(trace).getByText("run-1")).toBeVisible()
    expect(within(trace).getByText("revision-1")).toBeVisible()
    expect(within(trace).getByText("answered")).toBeVisible()
    expect(within(trace).getByText("evidence-1")).toBeVisible()
    expect(within(trace).queryByRole("link")).not.toBeInTheDocument()
    expect(mocks.fetchResearchOperation).toHaveBeenCalledWith(
      "request-selection-1",
      "gen_0123456789abcdef0123456789abcdef",
      expect.any(AbortSignal),
    )
    expect(mocks.previewPersonalMemoryTask).not.toHaveBeenCalled()
    expect(mocks.submitResearchAsk).not.toHaveBeenCalled()
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
        ...desktopTrace,
        runId: "run-1",
        paperId: paper.id,
        sourceRevision: "revision-1",
        answer: "这是绑定所选原文的解释。",
        citations: [{
          id: "citation-1",
          label: "Methods",
          evidenceId: "evidence-1",
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
    const onSelectionAnswered = vi.fn<NonNullable<ReaderAssistantProps["onSelectionAnswered"]>>()
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
        ...desktopTrace,
        runId: "run-1",
        paperId: paper.id,
        sourceRevision: "revision-1",
        answer: "旧选择回答",
        citations: [groundedCitation],
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
    const onSelectionAnswered = vi.fn<NonNullable<ReaderAssistantProps["onSelectionAnswered"]>>()
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
        ...desktopTrace,
        runId: "run-1",
        paperId: paper.id,
        sourceRevision: "revision-1",
        answer: "旧账号回答",
        citations: [groundedCitation],
        confidence: 0.8,
      },
    })

    await waitFor(() => expect(screen.queryByText("旧账号回答")).not.toBeInTheDocument())
    expect(onSelectionAnswered).not.toHaveBeenCalled()
  })

  it("shows the actual desktop execution receipt beside the answer without opening or executing it", async () => {
    const result = {
      ...groundedAnswerResult("可追溯回答"),
      citations: [groundedCitation, { ...groundedCitation, id: "citation-2", evidenceId: "evidence-2", page: 4 }],
      evidenceRefs: ["evidence-1", "evidence-2"],
    }
    mocks.submitResearchAsk.mockResolvedValue(groundedOperation(result))
    render(<Harness />)
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("请解释这段原文"))
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }))

    await screen.findByText("可追溯回答")
    const trace = screen.getByRole("region", { name: "回答执行追溯" })
    fireEvent.click(within(trace).getByText("回答执行追溯"))
    expect(within(trace).getByText("run-1")).toBeVisible()
    expect(within(trace).getByText("revision-1")).toBeVisible()
    expect(within(trace).getByText(desktopTrace.transcriptId)).toBeVisible()
    expect(within(trace).getByText("answered")).toBeVisible()
    expect(within(trace).getByText("evidence-1")).toBeVisible()
    expect(within(trace).getByText("evidence-2")).toBeVisible()
    expect(within(trace).getAllByRole("listitem").map(item => item.textContent)).toEqual(["evidence-1", "evidence-2"])
    expect(within(trace).queryByRole("link")).not.toBeInTheDocument()
    expect(within(trace).queryByRole("button")).not.toBeInTheDocument()
    expect(mocks.submitResearchAsk).toHaveBeenCalledTimes(1)
    expect(mocks.fetchResearchOperation).not.toHaveBeenCalled()
  })

  it.each([
    { label: "missing transcript", patch: { transcriptId: undefined } },
    { label: "null transcript", patch: { transcriptId: null } },
    { label: "missing answer status", patch: { answerStatus: undefined } },
    { label: "non-answer status", patch: { answerStatus: "succeeded" } },
    { label: "missing evidence refs", patch: { evidenceRefs: undefined } },
    { label: "empty evidence refs", patch: { evidenceRefs: [] } },
    { label: "duplicate evidence refs", patch: { evidenceRefs: ["evidence-1", "evidence-1"] } },
    { label: "uncited evidence refs", patch: { evidenceRefs: ["other-evidence"] } },
    { label: "wrong run", patch: { runId: "another-run" } },
    { label: "wrong source revision", patch: { sourceRevision: "another-revision" } },
    { label: "raw provider metadata", patch: { metadata: { apiKey: "test-only-secret" } } },
  ])("does not display a successful answer or trace for a $label receipt", async ({ patch }) => {
    const result = { ...groundedAnswerResult("不可采纳的回答"), ...patch }
    mocks.submitResearchAsk.mockResolvedValue(groundedOperation(result))
    const onSelectionAnswered = vi.fn<NonNullable<ReaderAssistantProps["onSelectionAnswered"]>>()
    render(<Harness onSelectionAnswered={onSelectionAnswered} />)
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("请解释这段原文"))
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }))

    await screen.findByText("问答已完成，但返回内容无法安全呈现。请从运行记录核对结果。")
    expect(screen.queryByText("不可采纳的回答")).not.toBeInTheDocument()
    expect(screen.queryByRole("region", { name: "回答执行追溯" })).not.toBeInTheDocument()
    expect(onSelectionAnswered).not.toHaveBeenCalled()
  })

  it("removes an earlier successful answer and trace when rechecking returns an invalid receipt", async () => {
    const result = groundedAnswerResult("先前成功回答")
    mocks.submitResearchAsk.mockResolvedValue(groundedOperation(result))
    mocks.fetchResearchOperation.mockResolvedValue(groundedOperation({ ...result, transcriptId: null }))
    const onSelectionAnswered = vi.fn<NonNullable<ReaderAssistantProps["onSelectionAnswered"]>>()
    render(<Harness onSelectionAnswered={onSelectionAnswered} />)
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("请解释这段原文"))
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }))
    await screen.findByText("先前成功回答")
    expect(screen.getByRole("region", { name: "回答执行追溯" })).toBeInTheDocument()
    expect(onSelectionAnswered).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole("button", { name: "检查已有问答请求" }))

    await screen.findByText("问答已完成，但返回内容无法安全呈现。请从运行记录核对结果。")
    expect(screen.queryByText("先前成功回答")).not.toBeInTheDocument()
    expect(screen.queryByRole("region", { name: "回答执行追溯" })).not.toBeInTheDocument()
    expect(onSelectionAnswered).toHaveBeenCalledTimes(1)
    expect(mocks.submitResearchAsk).toHaveBeenCalledTimes(1)
    expect(mocks.fetchResearchOperation).toHaveBeenCalledTimes(1)
  })

  it("shows a legacy public answer without inventing a desktop execution trace", async () => {
    mocks.hostedProfile = false
    const publicAnswer = {
      paperId: paper.id,
      question: "请解释这段原文",
      answer: "旧公共接口回答",
      citations: [],
      confidence: 0.7,
      generatedAt: "2026-10-15T00:00:00Z",
      cached: false,
      locale: "zh",
    } satisfies PaperReaderAnswer
    mocks.askPaper.mockResolvedValue(publicAnswer)
    render(<Harness />)
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("请解释这段原文"))
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }))

    await screen.findByText("旧公共接口回答")
    expect(screen.queryByRole("region", { name: "回答执行追溯" })).not.toBeInTheDocument()
    expect(screen.queryByText(desktopTrace.transcriptId)).not.toBeInTheDocument()
    expect(mocks.askPaper).toHaveBeenCalledTimes(1)
    expect(mocks.fetchDesktopIdentity).not.toHaveBeenCalled()
    expect(mocks.submitResearchAsk).not.toHaveBeenCalled()
  })
})
