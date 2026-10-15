import { describe, expect, it } from "vitest"
import { answerFromResult, recoverSelectionRequest, safeReaderSourceUrl } from "./reader-assistant"

describe("safeReaderSourceUrl", () => {
  it("allows public HTTP sources and the declared owner-scoped desktop asset path", () => {
    expect(safeReaderSourceUrl("https://arxiv.org/pdf/2601.01234")).toBe(
      "https://arxiv.org/pdf/2601.01234",
    )
    expect(safeReaderSourceUrl("/api/v1/research/desktop/papers/2601.01234/source?sourceRevision=revision-1&sourceFingerprint=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")).toBe(
      "/api/v1/research/desktop/papers/2601.01234/source?sourceRevision=revision-1&sourceFingerprint=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
  })

  it("rejects credentials, fragments, duplicate identity and unrelated same-origin routes", () => {
    const fingerprint = `sha256:${"a".repeat(64)}`
    expect(safeReaderSourceUrl("file:///private/paper.pdf")).toBeUndefined()
    expect(safeReaderSourceUrl("https://user:secret@example.test/paper.pdf")).toBeUndefined()
    expect(safeReaderSourceUrl("/api/admin/secrets")).toBeUndefined()
    expect(safeReaderSourceUrl(`/api/v1/research/desktop/papers/%2Fetc/source?sourceRevision=one&sourceFingerprint=${fingerprint}`)).toBeUndefined()
    expect(safeReaderSourceUrl(`/api/v1/research/desktop/papers/paper-1/source?sourceRevision=one&sourceRevision=two&sourceFingerprint=${fingerprint}`)).toBeUndefined()
    expect(safeReaderSourceUrl(`/api/v1/research/desktop/papers/paper-1/source?sourceRevision=one&sourceFingerprint=${fingerprint}#page=2`)).toBeUndefined()
  })

  it("recovers a persisted grounded selection without inventing a second job", () => {
    const operation = {
      requestId: "request-grounded-1",
      generation: "gen_0123456789abcdef0123456789abcdef",
      kind: "ask" as const,
      fingerprint: `sha256:${"b".repeat(64)}`,
      semantic: {
        kind: "ask" as const,
        runId: "run-1",
        paperId: "2601.01234",
        sourceRevision: "revision-1",
        sourceFingerprint: `sha256:${"a".repeat(64)}`,
        question: "请解释这段原文",
        selection: { sectionId: "methods", page: 3, selectedText: "关键原文" },
        locale: "zh",
      },
      status: "unknown" as const,
      paperId: "2601.01234",
      sourceRevision: "revision-1",
      question: "请解释这段原文",
      createdAt: 1,
      updatedAt: 1,
    }
    expect(recoverSelectionRequest(operation, "alice")).toMatchObject({
      requestKey: "request-grounded-1",
      owner: "alice",
      sourceRevision: "revision-1",
      mode: "explain",
      selection: { sectionId: "methods", page: 3, selectedText: "关键原文" },
    })
  })
})


describe("desktop answer execution trace mapping", () => {
  function receipt() {
    return {
      paperId: "2601.01234",
      runId: "run-1",
      sourceRevision: "revision-1",
      answer: "A bounded grounded answer.",
      citations: [{
        id: "citation-1",
        label: "Methods",
        evidenceId: "evidence-1",
        sectionId: "methods",
        page: 3,
        valid: true,
      }],
      confidence: 1,
      generatedAt: "2026-10-15T00:00:00Z",
      transcriptId: "rag-transcript://paper_rag_session:0123456789abcdef/1a2b3c4d",
      answerStatus: "answered" as const,
      evidenceRefs: ["evidence-1"],
    }
  }

  it("retains the complete real receipt in the PaperReaderAnswer model", () => {
    const result = receipt()
    const answer = answerFromResult(result, result.paperId, "What is the method?", result.runId, result.sourceRevision)

    expect(answer).toMatchObject({
      paperId: result.paperId,
      question: "What is the method?",
      answer: result.answer,
      generatedAt: result.generatedAt,
      executionTrace: {
        runId: result.runId,
        sourceRevision: result.sourceRevision,
        transcriptId: result.transcriptId,
        answerStatus: result.answerStatus,
        evidenceRefs: result.evidenceRefs,
      },
    })
    expect(answer?.executionTrace).toEqual({
      runId: result.runId,
      sourceRevision: result.sourceRevision,
      transcriptId: result.transcriptId,
      answerStatus: result.answerStatus,
      evidenceRefs: result.evidenceRefs,
    })
    expect(answer?.citations[0]).toMatchObject({
      evidenceId: "evidence-1",
      sectionId: "methods",
      page: 3,
      sourceRevision: "revision-1",
      valid: true,
    })
    // Mutating a transport object later must not change an already rendered receipt.
    result.evidenceRefs.push("late-evidence")
    expect(answer?.executionTrace?.evidenceRefs).toEqual(["evidence-1"])
  })

  it.each(["transcriptId", "answerStatus", "evidenceRefs"])("refuses an incomplete %s rather than inventing trace data", field => {
    const result: Record<string, unknown> = { ...receipt() }
    delete result[field]
    expect(answerFromResult(result, "2601.01234", "Question", "run-1", "revision-1")).toBeNull()
  })

  it.each([
    { label: "null transcript", patch: { transcriptId: null } },
    { label: "empty transcript", patch: { transcriptId: "" } },
    { label: "non-answer status", patch: { answerStatus: "succeeded" } },
    { label: "failed answer status", patch: { answerStatus: "failed" } },
    { label: "empty evidence refs", patch: { evidenceRefs: [] } },
    { label: "duplicate evidence refs", patch: { evidenceRefs: ["evidence-1", "evidence-1"] } },
    { label: "uncited evidence refs", patch: { evidenceRefs: ["other-evidence"] } },
    { label: "raw metadata", patch: { metadata: { apiKey: "test-only-secret" } } },
  ])("refuses a $label before creating a successful answer model", ({ patch }) => {
    expect(answerFromResult({ ...receipt(), ...patch }, "2601.01234", "Question", "run-1", "revision-1")).toBeNull()
  })

  it.each([
    { paperId: "another-paper", runId: "run-1", sourceRevision: "revision-1" },
    { paperId: "2601.01234", runId: "another-run", sourceRevision: "revision-1" },
    { paperId: "2601.01234", runId: undefined, sourceRevision: "revision-1" },
    { paperId: "2601.01234", runId: "run-1", sourceRevision: "another-revision" },
  ])("does not attach a real receipt to a different active paper/run/revision ($paperId/$runId/$sourceRevision)", identity => {
    expect(answerFromResult(receipt(), identity.paperId, "Question", identity.runId, identity.sourceRevision)).toBeNull()
  })
})
