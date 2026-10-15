import { afterEach, describe, expect, it, vi } from "vitest"
import { fetchResearchOperation, previewPersonalMemoryTask, submitResearchAsk } from "./client"
import { groundedAnswerSchema } from "./contracts"

const memoryId = `pmem_${"1".repeat(32)}`
const policy = {
  policy: {
    allow_remote_processing: true,
    revision: 2,
    created_at: "2026-10-14T00:00:00+00:00",
    updated_at: "2026-10-14T00:01:00+00:00",
  },
}
const recall = {
  records: [{
    memory_id: memoryId,
    content: "Prefer methods-first explanations.",
    category: "reading_preference",
    source_refs: [{
      source_type: "user_input",
      source_ref: "user://preference/methods-first",
      source_revision: null,
      locator: null,
      label: null,
    }],
    status: "active",
    revision: 3,
    created_at: "2026-10-14T00:00:00+00:00",
    updated_at: "2026-10-14T00:00:00+00:00",
    origin: "user_confirmed",
    confirmed_at: "2026-10-14T00:00:00+00:00",
    disabled_at: null,
    deleted_at: null,
    score: 4,
    reasons: ["matched query terms"],
  }],
  used_memory_refs: [{ memory_id: memoryId, revision: 3 }],
  query: "bounded retrieval methods",
  memory_enabled: true,
}

function response(data: unknown) {
  return Promise.resolve(new Response(JSON.stringify({ success: true, data }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }))
}

describe("personal memory task preview", () => {
  afterEach(() => vi.unstubAllGlobals())

  it("uses the real local policy and bounded recall routes to freeze exact refs", async () => {
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => response(policy))
      .mockImplementationOnce(() => response(recall))
    vi.stubGlobal("fetch", fetchMock)

    const preview = await previewPersonalMemoryTask({
      query: "bounded retrieval methods",
      limit: 4,
      useMemory: true,
    })

    expect(preview.reason).toBe("matched")
    expect(preview.selection).toEqual({
      policyRevision: 2,
      refs: [{ memoryId, revision: 3 }],
    })
    expect(fetchMock.mock.calls[0][0]).toBe("/api/research/personal-memory/policy")
    expect(fetchMock.mock.calls[1][0]).toBe("/api/research/personal-memory/recall")
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
      query: "bounded retrieval methods",
      limit: 4,
      use_memory: true,
    })
  })

  it("does not recall or create a selection when this task is switched off", async () => {
    const fetchMock = vi.fn(() => response(policy))
    vi.stubGlobal("fetch", fetchMock)

    const preview = await previewPersonalMemoryTask({
      query: "bounded retrieval methods",
      useMemory: false,
    })

    expect(preview).toMatchObject({ reason: "task_disabled", recall: null })
    expect(preview.selection).toBeUndefined()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("does not recall when remote processing is disabled", async () => {
    const fetchMock = vi.fn(() => response({
      policy: { ...policy.policy, allow_remote_processing: false },
    }))
    vi.stubGlobal("fetch", fetchMock)

    const preview = await previewPersonalMemoryTask({
      query: "bounded retrieval methods",
      useMemory: true,
    })

    expect(preview).toMatchObject({ reason: "policy_disabled", recall: null })
    expect(preview.selection).toBeUndefined()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("omits task memory when recall has no relevant match", async () => {
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => response(policy))
      .mockImplementationOnce(() => response({
        ...recall,
        records: [],
        used_memory_refs: [],
      }))
    vi.stubGlobal("fetch", fetchMock)

    const preview = await previewPersonalMemoryTask({
      query: "unmatched topic",
      useMemory: true,
    })

    expect(preview.reason).toBe("no_match")
    expect(preview.selection).toBeUndefined()
  })
})


describe("desktop grounded answer transport", () => {
  afterEach(() => vi.unstubAllGlobals())

  const generation = "gen_0123456789abcdef0123456789abcdef"
  const fingerprint = `sha256:${"a".repeat(64)}`
  const answer = {
    runId: "run-1",
    paperId: "2601.01234",
    sourceRevision: "revision-1",
    answer: "A bounded answer grounded in the methods section.",
    citations: [{
      id: "citation-1",
      label: "Methods",
      evidenceId: "evidence-1",
      sectionId: "methods",
      page: 3,
      valid: true,
    }],
    confidence: 1,
    evidenceRefs: ["evidence-1"],
    transcriptId: "rag-transcript://paper_rag_session:0123456789abcdef/1a2b3c4d",
    answerStatus: "answered",
  }

  function operation(result: unknown) {
    return {
      requestId: "request-123",
      generation,
      fingerprint,
      kind: "ask",
      status: "passed",
      paperId: answer.paperId,
      runId: answer.runId,
      sourceRevision: answer.sourceRevision,
      recoverable: false,
      automaticRetryAllowed: false,
      result,
    }
  }

  it("preserves the service receipt across submit and renderer parsing without renaming status", async () => {
    const fetchMock = vi.fn().mockImplementation(() => response(operation(answer)))
    vi.stubGlobal("fetch", fetchMock)
    const body = {
      requestId: "request-123",
      generation,
      requestFingerprint: fingerprint,
      paperId: answer.paperId,
      runId: answer.runId,
      sourceRevision: answer.sourceRevision,
      sourceFingerprint: fingerprint,
      question: "What is the method?",
      selection: { sectionId: "methods" },
    }

    const submitted = await submitResearchAsk(body)

    expect(submitted.status).toBe("passed")
    expect(submitted.result).toEqual(answer)
    expect(groundedAnswerSchema.parse(submitted.result)).toEqual(answer)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe("/api/research/desktop/operations/ask")
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
    })
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(body)
  })

  it("preserves the identical transcript and evidence refs when recovering the original request", async () => {
    const fetchMock = vi.fn().mockImplementation(() => response(operation(answer)))
    vi.stubGlobal("fetch", fetchMock)

    const recovered = await fetchResearchOperation("request-123", generation)

    expect(recovered.result).toEqual(answer)
    expect(groundedAnswerSchema.parse(recovered.result)).toEqual(answer)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe(
      `/api/research/desktop/operations/request-123?generation=${generation}`,
    )
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      credentials: "same-origin",
      cache: "no-store",
    })
    expect(fetchMock.mock.calls[0][1].method).toBeUndefined()
  })

  it.each([
    { label: "missing", value: undefined },
    { label: "null", value: null },
  ])("does not invent a transcript for a $label receipt returned by transport", async ({ value }) => {
    const result: Record<string, unknown> = { ...answer }
    if (value === undefined) delete result.transcriptId
    else result.transcriptId = value
    const fetchMock = vi.fn().mockImplementation(() => response(operation(result)))
    vi.stubGlobal("fetch", fetchMock)

    const recovered = await fetchResearchOperation("request-123", generation)

    expect(recovered.result).toEqual(result)
    expect(() => groundedAnswerSchema.parse(recovered.result)).toThrow()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it.each([
    { label: "raw provider metadata", patch: { metadata: { apiKey: "test-only-secret" } } },
    { label: "duplicate refs", patch: { evidenceRefs: ["evidence-1", "evidence-1"] } },
    { label: "empty refs", patch: { evidenceRefs: [] } },
    { label: "unknown refs", patch: { evidenceRefs: ["other-evidence"] } },
    { label: "failed answer", patch: { answerStatus: "failed" } },
    { label: "raw status alias", patch: { status: "answered" } },
  ])("keeps transport separate while the renderer rejects $label", async ({ patch }) => {
    const result = { ...answer, ...patch }
    const fetchMock = vi.fn().mockImplementation(() => response(operation(result)))
    vi.stubGlobal("fetch", fetchMock)

    const recovered = await fetchResearchOperation("request-123", generation)

    expect(recovered.result).toEqual(result)
    expect(() => groundedAnswerSchema.parse(recovered.result)).toThrow()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
