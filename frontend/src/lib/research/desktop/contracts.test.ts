import { describe, expect, it } from "vitest"
import {
  desktopIdentitySchema,
  operationSchema,
  capabilitySchema,
  groundedAnswerSchema,
  parseDesktopReader,
  researchReportSchema,
  toResearchReportView,
} from "@/lib/research/desktop/contracts"

const generation = "gen_0123456789abcdef0123456789abcdef"
const fingerprint = `sha256:${"a".repeat(64)}`

describe("desktop research contracts", () => {
  it("requires the owner-bound generation contract", () => {
    const identity = desktopIdentitySchema.parse({
      owner: {
        userId: "alice",
        tenantId: null,
        memoryNamespace: "research:user:alice",
      },
      generation,
      requestContract: {
        fingerprint: "sha256-canonical-json-v1",
        requestId: "uuid",
        notFoundRetry: "query by request identity",
      },
    })
    expect(identity.generation).toBe(generation)
  })

  it("accepts the frozen UI operation projection", () => {
    const operation = operationSchema.parse({
      requestId: "request-123",
      generation,
      kind: "analyze",
      fingerprint,
      status: "needs_action",
      updatedAt: "2026-10-12T00:00:01Z",
      paperId: "2601.01234",
      errorCode: "quality_gate_failed",
      message: "Review required",
      recoverable: true,
      automaticRetryAllowed: false,
    })
    expect(operation.status).toBe("needs_action")
    expect(() => operationSchema.parse({ kind: "analyze", status: "running" })).toThrow()
  })

  it("preserves an honest needs-review reader without inventing source identity", () => {
    const reader = parseDesktopReader({
      paper: {
        id: "private-paper",
        slug: "private-paper",
        title: "Private paper",
        authors: [],
        abstract: "",
        publishedAt: "",
        topics: [],
        taskRefs: [],
        methodRefs: [],
        isPublished: false,
      },
      reader: null,
      state: "needs_review",
      reasonCode: "source_identity_unavailable",
      aiReady: false,
      capabilities: { ask: false, analyze: false },
    })
    expect(reader.state).toBe("needs_review")
    expect(reader.sourceRevision).toBeUndefined()
    expect(reader.sourceFingerprint).toBeUndefined()
  })

  it("derives export eligibility only from the backend authority projection", () => {
    const report = researchReportSchema.parse({
      schema: "newsroom.research-report/v1",
      runId: "run-1",
      paperId: "2601.01234",
      sourceRevision: "revision-1",
      status: "succeeded",
      disposition: "candidate",
      analysis: {
        paper_id: "2601.01234",
        summary: {
          problem: "Candidate problem",
          core_idea: "Candidate core idea",
          key_contributions: [],
          method_summary: "",
          experiment_summary: "",
          limitations: [],
          why_it_matters: "",
          read_next: [],
          evidenceIds: [],
          confidence: 0.8,
          quality_flags: [],
        },
        contributions: [],
        methods: [],
        experiments: [],
        limitations: [],
        reproducibility: [],
        related_work: [],
        claims: [],
        evidence_pack_id: "pack-1",
        quality: {},
      },
      quality: {
        result_id: "quality-1",
        target_id: "run-1",
        target_type: "reader_payload",
        passed: true,
        score: 1,
        gate_results: [],
        quality_flags: [],
      },
      citations: [],
      artifactRefs: [],
      traceRef: null,
      diagnostics: { artifactStatus: "valid", artifactReason: "" },
      eligibility: {
        accepted: false,
        qualityPassed: true,
        acceptedExport: false,
      },
    })
    const view = toResearchReportView(report)
    expect(view.summary).toBe("Candidate problem")
    expect(view.acceptedExport).toBe(false)
  })
  it("accepts bounded business-operation capability provenance", () => {
    const capability = capabilitySchema.parse({
      id: "pdf-import",
      label: "PDF import",
      required: true,
      configured: true,
      reachable: true,
      verified: true,
      verifiedAt: "2026-10-12T00:00:00Z",
      reasonCode: null,
      message: null,
      recovery: null,
      provenance: {
        verificationKind: "business_operation",
        operation: "import_pdf",
        authorityId: "imp_123",
      },
    })
    expect(capability.provenance?.authorityId).toBe("imp_123")
    expect(() => capabilitySchema.parse({ ...capability, provenance: { verificationKind: "probe", operation: "ok" } })).toThrow()
  })

  it("parses the actual controlled public report projection and rejects private metadata", () => {
    const projection = {
      schema: "newsroom.research-report/v1" as const,
      runId: "run-public-1",
      paperId: "2401.01234",
      sourceRevision: `sha256:${"a".repeat(64)}`,
      status: "succeeded",
      disposition: "accepted",
      analysis: {
        paper_id: "2401.01234",
        summary: {
          problem: "Ground the answer in the imported source.",
          core_idea: "A source-grounded method.",
          key_contributions: ["Exact source identity"],
          method_summary: "Retrieve evidence before generation.",
          experiment_summary: "Evaluated against the accepted source.",
          limitations: ["One source revision"],
          why_it_matters: "Prevents cross-revision claims.",
          read_next: [],
          evidenceIds: ["evidence:0123456789abcdef01234567"],
          confidence: 0.9,
          quality_flags: [],
        },
        contributions: ["Exact source identity"],
        methods: ["Retrieve evidence before generation."],
        experiments: ["Revision-bound evaluation"],
        limitations: ["One source revision"],
        reproducibility: ["Persist the request fingerprint"],
        related_work: [],
        claims: [],
        evidence_pack_id: "pack-public-1",
        quality: { evidence_item_count: 1, llm_candidate_warnings: [] },
      },
      quality: {
        result_id: "quality-public-1",
        target_id: "2401.01234",
        target_type: "reader_payload",
        passed: true,
        score: 0.9,
        gate_results: [],
        quality_flags: [],
      },
      citations: [{
        id: "citation:0123456789abcdef01234567",
        label: "methods",
        paperId: "2401.01234",
        sourceRevision: `sha256:${"a".repeat(64)}`,
        valid: true,
        sectionId: "methods",
      }],
      artifactRefs: [],
      traceRef: null,
      diagnostics: { artifactStatus: "valid" as const, artifactReason: "" },
      eligibility: { accepted: true, qualityPassed: true, acceptedExport: true },
    }
    expect(toResearchReportView(researchReportSchema.parse(projection)).sections).toEqual(
      expect.arrayContaining([expect.objectContaining({ id: "methods" })]),
    )
    expect(() => researchReportSchema.parse({
      ...projection,
      analysis: { ...projection.analysis, metadata: { path: "file:///C:/private.pdf" } },
    })).toThrow()
    expect(() => researchReportSchema.parse({
      ...projection,
      analysis: {},
    })).toThrow()
    expect(() => researchReportSchema.parse({
      ...projection,
      analysis: null,
      quality: null,
    })).toThrow()
    expect(() => researchReportSchema.parse({
      ...projection,
      artifactRefs: ["file:///C:/private.pdf"],
    })).toThrow()
    expect(() => researchReportSchema.parse({
      ...projection,
      quality: { ...projection.quality, target_id: "another-paper" },
    })).toThrow()
  })

  it("accepts only the safe grounded-answer projection", () => {
    const answer = {
      runId: "run-1",
      paperId: "2601.01234",
      sourceRevision: "revision-1",
      answer: "A grounded answer.",
      citations: [{
        id: "citation-1",
        label: "Methods",
        evidenceId: "evidence-1",
        sectionId: "methods",
        page: 3,
        excerpt: "bounded source text",
        valid: true,
        reasonCode: null,
      }],
      confidence: 0.9,
      generatedAt: "2026-10-12T00:00:00Z",
    }
    expect(groundedAnswerSchema.parse(answer).sourceRevision).toBe("revision-1")
    expect(() => groundedAnswerSchema.parse({
      ...answer,
      metadata: { sourceRefs: ["file:///C:/private.pdf"] },
    })).toThrow()
    expect(() => groundedAnswerSchema.parse({
      ...answer,
      citations: [{ ...answer.citations[0], url: "file:///C:/private.pdf" }],
    })).toThrow()
  })

})
