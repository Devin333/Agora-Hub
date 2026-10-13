import { describe, expect, it } from "vitest"
import { recoverSelectionRequest, safeReaderSourceUrl } from "./reader-assistant"

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
