import { readFileSync } from "node:fs"
import path from "node:path"
import { describe, expect, it } from "vitest"
import {
  buildAnalyzeRequest,
  buildAskRequest,
  buildPrepareRequest,
  sanitizeAnalyzeRequest,
  sanitizeAskRequest,
  sanitizePrepareRequest,
  normalizeAnalyzeTransportUrl,
} from "@/lib/research/desktop/request-contracts"
import type {
  ResearchAnalyzeSemantic,
  ResearchAskSemantic,
  ResearchPrepareSemantic,
} from "@/lib/research/workspace-items"

const generation = "gen_0123456789abcdef0123456789abcdef"
const fingerprint = `sha256:${"a".repeat(64)}`
const fixtures = JSON.parse(readFileSync(path.resolve(process.cwd(), "../tests/fixtures/research_desktop_request_contracts.json"), "utf8")) as {
  cases: Array<{ fingerprint: string; semantic: ResearchAnalyzeSemantic | ResearchAskSemantic | ResearchPrepareSemantic | { kind: "import" } }>
}

const identity = {
  requestId: "request-1",
  generation,
  fingerprint,
}

describe("desktop operation BFF boundary", () => {
  it("rejects browser-supplied owner scope", () => {
    expect(
      sanitizeAnalyzeRequest({
        ...buildAnalyzeRequest(identity, analyzeSemantic()),
        tenantId: "forged",
      }),
    ).toBeNull()
  })

  it("accepts the exact public prepare payload without semantic kind or owner scope", () => {
    const semantic: ResearchPrepareSemantic = {
      kind: "prepare",
      paperId: "2601.01234",
      sourceUrl: "https://arxiv.org/abs/2601.01234",
      sourceType: null,
    }
    const request = buildPrepareRequest(identity, semantic)
    expect(request).not.toHaveProperty("kind")
    expect(sanitizePrepareRequest(request)).toEqual(request)
    expect(sanitizePrepareRequest({ ...request, userId: "forged" })).toBeNull()
    expect(sanitizePrepareRequest({ ...request, sourceUrl: "/private/source" })).toBeNull()
  })

  it("accepts the exact analyze payload built by the reader without semantic kind", () => {
    const request = buildAnalyzeRequest(identity, analyzeSemantic())
    expect(request).not.toHaveProperty("kind")
    expect(request.paperId).toBe("2601.01234")
    expect(sanitizeAnalyzeRequest(request)).toEqual(request)
  })

  it("converts the controlled private reader URL to null for analyze transport", () => {
    const privateSource = `/api/v1/research/desktop/papers/2601.01234/source?sourceRevision=revision-1&sourceFingerprint=${fingerprint}`
    expect(normalizeAnalyzeTransportUrl(privateSource)).toBeNull()
    const request = buildAnalyzeRequest(identity, {
      ...analyzeSemantic(),
      sourceUrl: privateSource,
      pdfUrl: privateSource,
    })
    expect(request.sourceUrl).toBeNull()
    expect(request.pdfUrl).toBeNull()
    expect(sanitizeAnalyzeRequest(request)).toEqual(request)
  })

  it("accepts a private imported source resolved by exact owner identity", () => {
    const semantic = analyzeSemantic()
    const request = buildAnalyzeRequest(identity, {
      ...semantic,
      sourceUrl: null,
      pdfUrl: null,
    })
    expect(sanitizeAnalyzeRequest(request)).toEqual(request)
  })

  it("accepts the exact ask payload built by the reader and rejects missing selection", () => {
    const request = buildAskRequest(identity, askSemantic())
    expect(request).not.toHaveProperty("kind")
    expect(sanitizeAskRequest(request)).toEqual(request)
    const missingSelection = { ...request }
    delete (missingSelection as Partial<typeof request>).selection
    expect(sanitizeAskRequest(missingSelection)).toBeNull()
  })
  it("allows only exact personal-memory refs and rejects forged owner or content", () => {
    const personalMemory = {
      refs: [{ memoryId: `pmem_${"1".repeat(32)}`, revision: 3 }],
      policyRevision: 2,
    }
    const analyze = buildAnalyzeRequest(identity, {
      ...analyzeSemantic(),
      personalMemory,
    })
    const ask = buildAskRequest(identity, {
      ...askSemantic(),
      personalMemory,
    })

    expect(sanitizeAnalyzeRequest(analyze)).toEqual(analyze)
    expect(sanitizeAskRequest(ask)).toEqual(ask)
    expect(sanitizeAskRequest({
      ...ask,
      personalMemory: { ...personalMemory, owner: "forged" },
    })).toBeNull()
    expect(sanitizeAskRequest({
      ...ask,
      personalMemory: {
        ...personalMemory,
        refs: [{ ...personalMemory.refs[0], content: "forged" }],
      },
    })).toBeNull()
    expect(sanitizeAskRequest({ ...ask, personalMemory: null })).toBeNull()
  })

  it("preserves a null objective and rejects blank constraints", () => {
    const request = buildAnalyzeRequest(identity, {
      ...analyzeSemantic(),
      objective: null,
    })
    expect(request.objective).toBeNull()
    expect(sanitizeAnalyzeRequest(request)).toEqual(request)
    expect(sanitizeAnalyzeRequest({ ...request, constraints: [""] })).toBeNull()
    expect(sanitizeAnalyzeRequest({ ...request, constraints: ["   "] })).toBeNull()
  })

  it("accepts the shared Unicode prepare, analyze, and ask semantics at the BFF boundary", () => {
    for (const fixture of fixtures.cases) {
      if (fixture.semantic.kind === "analyze") {
        const request = buildAnalyzeRequest(
          { ...identity, fingerprint: fixture.fingerprint },
          fixture.semantic,
        )
        expect(sanitizeAnalyzeRequest(request)).toEqual(request)
      }
      if (fixture.semantic.kind === "prepare") {
        const request = buildPrepareRequest(
          { ...identity, fingerprint: fixture.fingerprint },
          fixture.semantic,
        )
        expect(sanitizePrepareRequest(request)).toEqual(request)
      }
      if (fixture.semantic.kind === "ask") {
        const request = buildAskRequest(
          { ...identity, fingerprint: fixture.fingerprint },
          fixture.semantic,
        )
        expect(sanitizeAskRequest(request)).toEqual(request)
      }
    }
  })

})

function analyzeSemantic(): ResearchAnalyzeSemantic {
  return {
    kind: "analyze",
    paperId: "2601.01234",
    sourceRevision: "revision-1",
    sourceFingerprint: fingerprint,
    sourceUrl: null,
    pdfUrl: "https://example.test/paper.pdf",
    objective: "Analyze paper 2601.01234",
    constraints: [],
  }
}

function askSemantic(): ResearchAskSemantic {
  return {
    kind: "ask",
    runId: "run-1",
    paperId: "2601.01234",
    sourceRevision: "revision-1",
    sourceFingerprint: fingerprint,
    question: "为什么？",
    selection: { sectionId: "methods", page: 3 },
    locale: "zh",
  }
}
