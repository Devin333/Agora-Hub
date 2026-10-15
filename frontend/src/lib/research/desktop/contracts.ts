import { z } from "zod"
import type { ReaderWorkspacePayload } from "@/features/portal/reader/reader-contract"

const boundedText = (maximum: number) => z.string().max(maximum)
const identifier = z.string().min(1).max(256)
const fingerprint = z.string().regex(/^sha256:[0-9a-f]{64}$/)
const generation = z.string().regex(/^gen_[0-9a-f]{32}$/)
const publicHttpUrl = boundedText(4000).refine(value => {
  try {
    const url = new URL(value)
    return (url.protocol === "http:" || url.protocol === "https:") && !url.username && !url.password
  } catch {
    return false
  }
}, "Expected a public HTTP(S) URL")

export const desktopIdentitySchema = z
  .object({
    owner: z
      .object({
        userId: identifier,
        tenantId: boundedText(256).nullable().optional(),
        memoryNamespace: boundedText(512),
      })
      .strict(),
    generation,
    requestContract: z
      .object({
        fingerprint: z.literal("sha256-canonical-json-v1"),
        requestId: boundedText(200),
        notFoundRetry: boundedText(200),
      })
      .strict(),
  })
  .strict()

export type DesktopIdentity = z.infer<typeof desktopIdentitySchema>

const capabilityProvenanceSchema = z
  .object({
    verificationKind: z.literal("business_operation"),
    operation: boundedText(200),
    authorityId: boundedText(256).optional(),
  })
  .strict()

export const capabilitySchema = z
  .object({
    id: identifier,
    label: boundedText(200),
    required: z.boolean(),
    configured: z.boolean(),
    reachable: z.boolean(),
    verified: z.boolean(),
    verifiedAt: boundedText(100).nullable().optional(),
    reasonCode: boundedText(200).nullable().optional(),
    message: boundedText(1000).nullable().optional(),
    recovery: boundedText(2000).nullable().optional(),
    provenance: capabilityProvenanceSchema.nullable(),
  })
  .strict()

export const desktopCapabilitiesSchema = z
  .object({
    generation,
    capabilities: z.array(capabilitySchema).max(50),
  })
  .strict()

export type DesktopCapabilities = z.infer<typeof desktopCapabilitiesSchema>

export const personalMemoryRefSchema = z
  .object({
    memoryId: z.string().regex(/^pmem_[0-9a-f]{32}$/),
    revision: z.number().int().positive(),
  })
  .strict()

export const personalMemorySelectionSchema = z
  .object({
    refs: z.array(personalMemoryRefSchema).min(1).max(20),
    policyRevision: z.number().int().positive(),
  })
  .strict()
  .superRefine((selection, context) => {
    if (new Set(selection.refs.map(item => item.memoryId)).size !== selection.refs.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["refs"],
        message: "Personal memory refs must be unique",
      })
    }
  })

export type PersonalMemoryRef = z.infer<typeof personalMemoryRefSchema>
export type PersonalMemorySelection = z.infer<typeof personalMemorySelectionSchema>

const personalMemorySourceRefSchema = z
  .object({
    source_type: z.enum([
      "user_input", "reading_note", "paper", "evidence",
      "conversation", "import", "other",
    ]),
    source_ref: boundedText(1024),
    source_revision: boundedText(256).nullable(),
    locator: boundedText(512).nullable(),
    label: boundedText(256).nullable(),
  })
  .strict()

const recalledPersonalMemoryRecordSchema = z
  .object({
    memory_id: z.string().regex(/^pmem_[0-9a-f]{32}$/),
    content: boundedText(8000),
    category: z.enum(["research_interest", "reading_preference", "background"]),
    source_refs: z.array(personalMemorySourceRefSchema).min(1).max(32),
    status: z.literal("active"),
    revision: z.number().int().positive(),
    created_at: boundedText(100),
    updated_at: boundedText(100),
    origin: z.enum(["model_candidate", "user_confirmed"]),
    confirmed_at: boundedText(100),
    disabled_at: z.null(),
    deleted_at: z.null(),
    score: z.number().int(),
    reasons: z.array(boundedText(200)).max(100),
  })
  .strict()

const recalledPersonalMemoryRefSchema = z
  .object({
    memory_id: z.string().regex(/^pmem_[0-9a-f]{32}$/),
    revision: z.number().int().positive(),
  })
  .strict()

export const personalMemoryRecallSchema = z
  .object({
    records: z.array(recalledPersonalMemoryRecordSchema).max(20),
    used_memory_refs: z.array(recalledPersonalMemoryRefSchema).max(20),
    query: boundedText(512),
    memory_enabled: z.boolean(),
  })
  .strict()

export const personalMemoryPolicySchema = z
  .object({
    policy: z
      .object({
        allow_remote_processing: z.boolean(),
        revision: z.number().int().positive(),
        created_at: boundedText(100),
        updated_at: boundedText(100),
      })
      .strict(),
  })
  .strict()

export type PersonalMemoryRecall = z.infer<typeof personalMemoryRecallSchema>
export type PersonalMemoryPolicy = z.infer<typeof personalMemoryPolicySchema>

export const operationSchema = z
  .object({
    requestId: z.string().min(8).max(128),
    generation,
    fingerprint,
    kind: z.enum(["prepare", "analyze", "ask", "import"]),
    status: z.enum([
      "waiting",
      "running",
      "needs_action",
      "passed",
      "rejected",
      "failed",
      "interrupted",
      "unknown",
    ]),
    updatedAt: boundedText(100).optional(),
    paperId: identifier.optional(),
    sourceRevision: boundedText(512).optional(),
    importId: identifier.optional(),
    runId: identifier.optional(),
    reportId: identifier.optional(),
    errorCode: boundedText(200).optional(),
    message: boundedText(2000).optional(),
    recoverable: z.boolean(),
    automaticRetryAllowed: z.literal(false),
    result: z.record(z.string(), z.unknown()).optional(),
  })
  .strict()

export type ResearchOperation = z.infer<typeof operationSchema>

// Trace identifiers are opaque references, not browser URLs or filesystem paths.
// Preserve their exact spelling: trimming, coercing, or truncating would change identity.
const answerEvidenceRefSchema = identifier.refine(
  value => !/\s|[\u0000-\u001f\u007f]/.test(value),
  "Expected a nonblank evidence identifier without whitespace or control characters",
)
const answerTranscriptRefSchema = identifier.refine(
  value => !/\s|[\u0000-\u001f\u007f]/.test(value),
  "Expected an opaque transcript reference without whitespace or control characters",
)

const groundedAnswerCitationSchema = z
  .object({
    id: boundedText(200),
    label: boundedText(500),
    evidenceId: answerEvidenceRefSchema,
    sectionId: boundedText(200).optional(),
    page: z.number().int().positive().max(100000).optional(),
    excerpt: boundedText(10000).optional(),
    valid: z.literal(true),
    reasonCode: boundedText(200).nullable().optional(),
  })
  .strict()
  .superRefine((citation, context) => {
    if (!citation.sectionId?.trim() && citation.page === undefined) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["sectionId"],
        message: "An accepted answer citation must have a section or page locator",
      })
    }
  })

export const groundedAnswerSchema = z
  .object({
    runId: identifier,
    paperId: identifier,
    sourceRevision: boundedText(512),
    answer: boundedText(100000).min(1).refine(value => value.trim().length > 0, "Expected a nonblank answer"),
    citations: z.array(groundedAnswerCitationSchema).min(1).max(1000),
    confidence: z.number().min(0).max(1),
    evidenceRefs: z.array(answerEvidenceRefSchema).min(1).max(1000),
    transcriptId: answerTranscriptRefSchema,
    // The desktop service rejects non-answer outcomes before projecting this receipt.
    answerStatus: z.literal("answered"),
    generatedAt: boundedText(100).optional(),
    usedPersonalMemory: z.array(personalMemoryRefSchema).max(20).optional(),
  })
  .strict()
  .superRefine((answer, context) => {
    if (new Set(answer.evidenceRefs).size !== answer.evidenceRefs.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["evidenceRefs"],
        message: "Answer evidence refs must be unique",
      })
    }

    const citedRefs = [...new Set(answer.citations.map(citation => citation.evidenceId))]
    if (
      citedRefs.length !== answer.evidenceRefs.length ||
      citedRefs.some((ref, index) => ref !== answer.evidenceRefs[index])
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["evidenceRefs"],
        message: "Answer evidence refs must match the ordered evidence IDs of valid citations",
      })
    }
  })


const citationSchema = z
  .object({
    id: boundedText(200),
    label: boundedText(500),
    paperId: identifier,
    sourceRevision: boundedText(512),
    valid: z.boolean(),
    sectionId: boundedText(200).optional(),
    page: z.number().int().positive().max(100000).optional(),
    excerpt: boundedText(10000).optional(),
    url: publicHttpUrl.optional(),
    reasonCode: boundedText(200).optional(),
  })
  .strict()

const publicQualityFlagSchema = z
  .object({
    flag_type: boundedText(200),
    severity: z.enum(["low", "medium", "high", "critical"]),
    message: boundedText(2000),
    evidenceIds: z.array(boundedText(256)).max(500),
  })
  .strict()

const publicThreeMinuteReadSchema = z
  .object({
    problem: boundedText(100000),
    core_idea: boundedText(100000),
    key_contributions: z.array(boundedText(10000)).max(500),
    method_summary: boundedText(100000),
    experiment_summary: boundedText(100000),
    limitations: z.array(boundedText(10000)).max(500),
    why_it_matters: boundedText(100000),
    read_next: z.array(boundedText(10000)).max(500),
    evidenceIds: z.array(boundedText(256)).max(1000),
    confidence: z.number().min(0).max(1),
    quality_flags: z.array(publicQualityFlagSchema).max(500),
  })
  .strict()

const publicResearchClaimSchema = z
  .object({
    claim_id: boundedText(256),
    text: boundedText(10000),
    claim_type: z.enum([
      "problem", "method", "experiment", "limitation", "sota",
      "reproducibility", "other",
    ]),
    section_id: boundedText(200).nullable(),
    evidence_ids: z.array(boundedText(256)).max(1000),
    confidence: z.number().min(0).max(1),
  })
  .strict()

const publicAnalysisQualitySchema = z
  .object({
    evidence_item_count: z.number().int().nonnegative().max(1_000_000).optional(),
    llm_candidate_warnings: z.array(boundedText(2000)).max(500).optional(),
  })
  .strict()

const researchAnalysisSchema = z
  .object({
    paper_id: identifier,
    summary: publicThreeMinuteReadSchema,
    contributions: z.array(boundedText(10000)).max(500),
    methods: z.array(boundedText(10000)).max(500),
    experiments: z.array(boundedText(10000)).max(500),
    limitations: z.array(boundedText(10000)).max(500),
    reproducibility: z.array(boundedText(10000)).max(500),
    related_work: z.array(boundedText(10000)).max(500),
    claims: z.array(publicResearchClaimSchema).max(1000),
    evidence_pack_id: boundedText(256),
    quality: publicAnalysisQualitySchema,
  })
  .strict()

const publicGateResultSchema = z
  .object({
    gate_name: boundedText(200),
    passed: z.boolean(),
    reasons: z.array(boundedText(2000)).max(500),
    quality_flags: z.array(publicQualityFlagSchema).max(500),
  })
  .strict()

const researchQualitySchema = z
  .object({
    result_id: boundedText(256),
    target_id: boundedText(256),
    target_type: boundedText(100),
    passed: z.boolean(),
    score: z.number().min(0).max(1),
    gate_results: z.array(publicGateResultSchema).max(500),
    quality_flags: z.array(publicQualityFlagSchema).max(500),
  })
  .strict()

const reportDiagnosticsSchema = z
  .object({
    artifactStatus: z.enum(["valid", "missing", "corrupt", "unauthorized"]),
    artifactReason: boundedText(200),
    reportId: boundedText(256).optional(),
    title: boundedText(500).optional(),
    generatedAt: boundedText(100).optional(),
  })
  .strict()

export const researchReportSchema = z
  .object({
    schema: z.literal("newsroom.research-report/v1"),
    runId: identifier,
    paperId: identifier,
    sourceRevision: boundedText(512),
    status: boundedText(100),
    disposition: boundedText(100).nullable(),
    analysis: researchAnalysisSchema.nullable(),
    quality: researchQualitySchema.nullable(),
    citations: z.array(citationSchema).max(1000),
    usedPersonalMemory: z.array(personalMemoryRefSchema).max(20).optional(),
    artifactRefs: z.array(z.never()).length(0),
    traceRef: z.null(),
    diagnostics: reportDiagnosticsSchema,
    eligibility: z
      .object({
        accepted: z.boolean(),
        qualityPassed: z.boolean(),
        acceptedExport: z.boolean(),
      })
      .strict(),
  })
  .strict()
  .superRefine((report, context) => {
    if (report.eligibility.accepted && (!report.analysis || !report.quality)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["eligibility", "accepted"],
        message: "Accepted reports require the complete analysis and quality projections",
      })
    }
    if (
      report.eligibility.accepted &&
      (report.analysis?.paper_id !== report.paperId || report.quality?.target_id !== report.paperId)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["paperId"],
        message: "Accepted report identities must match the projected paper",
      })
    }
    if (report.eligibility.qualityPassed && report.quality?.passed !== true) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["eligibility", "qualityPassed"],
        message: "Quality eligibility must match the typed quality result",
      })
    }
    if (
      report.eligibility.acceptedExport &&
      (!report.eligibility.accepted ||
        !report.eligibility.qualityPassed ||
        !report.analysis ||
        !report.quality)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["eligibility", "acceptedExport"],
        message: "Accepted export requires a complete accepted report",
      })
    }
  })

export type ResearchReport = z.infer<typeof researchReportSchema>

export const researchReportReferenceSchema = z
  .object({
    reportId: identifier,
    runId: identifier,
    paperId: identifier,
    sourceRevision: boundedText(512),
    title: boundedText(500),
    savedAt: boundedText(100),
    updatedAt: boundedText(100),
    metadata: z.record(z.string(), z.unknown()),
  })
  .strict()

export type ResearchReportReference = z.infer<typeof researchReportReferenceSchema>

export const researchReportListSchema = z
  .object({ reports: z.array(researchReportReferenceSchema).max(500) })
  .strict()

export const savedResearchReportSchema = z
  .object({
    reference: researchReportReferenceSchema,
    report: researchReportSchema,
  })
  .strict()

export type SavedResearchReport = z.infer<typeof savedResearchReportSchema>

export type ResearchReportSection = {
  id: string
  title: string
  paragraphs: string[]
  items: string[]
}

export type ResearchReportView = {
  id: string
  title: string
  runId: string
  paperId: string
  sourceRevision: string
  status: string
  disposition: string | null
  generatedAt?: string
  summary: string
  sections: ResearchReportSection[]
  quality: {
    passed: boolean
    score: number | null
    reasons: string[]
  }
  citations: ResearchReport["citations"]
  usedPersonalMemory: ResearchReport["usedPersonalMemory"]
  artifactStatus: ResearchReport["diagnostics"]["artifactStatus"]
  artifactReason: string
  accepted: boolean
  qualityPassed: boolean
  acceptedExport: boolean
  diagnosticJson: string
}

export function toResearchReportView(
  report: ResearchReport,
  reference?: ResearchReportReference,
): ResearchReportView {
  const analysis = report.analysis
  const summary = analysis?.summary
  const quality = report.quality
  return {
    id: reference?.reportId ?? report.diagnostics.reportId ?? report.runId,
    title:
      reference?.title ??
      report.diagnostics.title ??
      `Research 报告 · ${report.paperId}`,
    runId: report.runId,
    paperId: report.paperId,
    sourceRevision: report.sourceRevision,
    status: report.status,
    disposition: report.disposition,
    generatedAt: reference?.savedAt ?? report.diagnostics.generatedAt,
    summary: summary?.problem ?? "",
    sections: analysis
      ? [
          reportSection("problem", "研究问题", [summary?.problem], []),
          reportSection("core-idea", "核心思路", [summary?.core_idea], []),
          reportSection(
            "contributions",
            "主要贡献",
            [],
            uniqueTexts([...(summary?.key_contributions ?? []), ...(analysis.contributions ?? [])]),
          ),
          reportSection("methods", "方法", [summary?.method_summary], analysis.methods ?? []),
          reportSection(
            "experiments",
            "实验与结果",
            [summary?.experiment_summary],
            analysis.experiments ?? [],
          ),
          reportSection(
            "why-it-matters",
            "研究意义",
            [summary?.why_it_matters],
            [],
          ),
          reportSection(
            "limitations",
            "局限",
            [],
            uniqueTexts([...(summary?.limitations ?? []), ...(analysis.limitations ?? [])]),
          ),
          reportSection("reproducibility", "复现", [], analysis.reproducibility ?? []),
          reportSection("claims", "可核对结论", [], (analysis.claims ?? []).map(claim => claim.text)),
          reportSection("read-next", "继续阅读", [], summary?.read_next ?? []),
        ].filter(section => section.paragraphs.length > 0 || section.items.length > 0)
      : [],
    quality: {
      passed: quality?.passed === true,
      score: quality?.score ?? null,
      reasons: qualityReasons(quality),
    },
    citations: report.citations,
    usedPersonalMemory: report.usedPersonalMemory,
    artifactStatus: report.diagnostics.artifactStatus,
    artifactReason: report.diagnostics.artifactReason,
    accepted: report.eligibility.accepted,
    qualityPassed: report.eligibility.qualityPassed,
    acceptedExport: report.eligibility.acceptedExport,
    diagnosticJson: JSON.stringify(
      {
        status: report.status,
        disposition: report.disposition,
        diagnostics: report.diagnostics,
        eligibility: report.eligibility,
        artifactRefs: report.artifactRefs,
        traceRef: report.traceRef,
      },
      null,
      2,
    ),
  }
}

function reportSection(
  id: string,
  title: string,
  paragraphs: Array<string | undefined>,
  items: string[],
): ResearchReportSection {
  return {
    id,
    title,
    paragraphs: uniqueTexts(paragraphs.filter((value): value is string => Boolean(value?.trim()))),
    items: uniqueTexts(items),
  }
}

function qualityReasons(quality: ResearchReport["quality"]): string[] {
  if (!quality) return []
  return uniqueTexts([
    ...(quality.gate_results ?? []).flatMap(result =>
      result.passed
        ? []
        : [
            ...result.reasons,
            ...result.quality_flags.map(flag => flag.message),
          ],
    ),
    ...(quality.quality_flags ?? []).map(flag => flag.message),
  ])
}

function uniqueTexts(values: string[]): string[] {
  return [...new Set(values.map(value => value.trim()).filter(Boolean))]
}

export type DesktopReaderPayload = ReaderWorkspacePayload & {
  sourceRevision?: string
  sourceFingerprint?: string
  runId?: string
  capabilities?: {
    ask: boolean
    analyze: boolean
  }
}

export function parseDesktopReader(value: unknown): DesktopReaderPayload {
  if (!value || typeof value !== "object") throw new Error("reader_invalid_response")
  const item = value as Partial<DesktopReaderPayload>
  if (
    !item.paper ||
    typeof item.paper.id !== "string" ||
    !["ready", "unavailable", "needs_review"].includes(String(item.state))
  ) {
    throw new Error("reader_invalid_response")
  }
  if (
    item.state === "ready" &&
    (!item.reader ||
      typeof item.sourceRevision !== "string" ||
      !fingerprint.safeParse(item.sourceFingerprint).success)
  ) {
    throw new Error("reader_invalid_response")
  }
  if (
    item.state !== "ready" &&
    (item.capabilities?.ask === true || item.capabilities?.analyze === true)
  ) {
    throw new Error("reader_invalid_response")
  }

  const rawPaper = item.paper as typeof item.paper & {
    abstract?: string
    topics?: string[]
  }
  const paper = {
    ...rawPaper,
    slug: rawPaper.slug || rawPaper.id,
    title: rawPaper.title || rawPaper.id,
    abstractSnippet: rawPaper.abstractSnippet || rawPaper.abstract || "",
    publishedAt: rawPaper.publishedAt || "",
    tags: Array.isArray(rawPaper.tags)
      ? rawPaper.tags
      : Array.isArray(rawPaper.topics)
        ? rawPaper.topics
        : [],
    authors: Array.isArray(rawPaper.authors) ? rawPaper.authors : [],
    taskRefs: Array.isArray(rawPaper.taskRefs) ? rawPaper.taskRefs : [],
    methodRefs: Array.isArray(rawPaper.methodRefs) ? rawPaper.methodRefs : [],
    isPublished: rawPaper.isPublished === true,
  }
  return { ...item, paper } as DesktopReaderPayload
}
