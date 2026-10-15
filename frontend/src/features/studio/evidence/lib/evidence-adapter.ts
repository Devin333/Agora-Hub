import type {
  ClaimSupportStatus,
  StudioCitationFailureCategory,
  StudioClaimEvidence,
  StudioEvidenceCounts,
  StudioEvidenceDataState,
  StudioEvidenceOverview,
  StudioEvidenceRef,
  StudioEvidenceRunSummary,
  StudioEvidenceSourceRef,
  StudioLlmTrace,
  StudioRunEvidenceDetail
} from "@/types/evidence"

export type RawRunEvidenceInput = {
  runId?: string
  reportId?: string
  runListItem?: unknown
  runDetail?: unknown
  events?: unknown
  diagnostics?: unknown
  lineage?: unknown
  reportQuality?: unknown
  notices?: string[]
  dataState?: StudioEvidenceDataState
}

const STATUS_WEIGHT: Record<ClaimSupportStatus, number> = {
  accepted: 1,
  uncertain: 2,
  unsupported: 3,
  rejected: 4
}

const FALLBACK_RUN_ID = "fallback-evidence-run"

export function adaptRunEvidenceDetail(input: RawRunEvidenceInput): StudioRunEvidenceDetail {
  const runDetail = record(input.runDetail)
  const runListItem = record(input.runListItem)
  const manifest = record(runDetail?.manifest)
  const manifestOutput = record(manifest?.output)
  const outputPreview = record(runDetail?.output_preview) ?? record(runDetail?.outputPreview) ?? manifestOutput ?? {}
  const diagnosticsRoot = record(input.diagnostics)
  const diagnostics = record(diagnosticsRoot?.diagnostics) ?? diagnosticsRoot ?? {}
  const reportQualityRoot = record(input.reportQuality)
  const reportQuality = record(reportQualityRoot?.quality) ?? reportQualityRoot ?? {}
  const qualityTrace =
    record(outputPreview.quality_trace) ??
    record(outputPreview.qualityTrace) ??
    record(diagnostics.quality_trace) ??
    record(reportQuality.quality_trace) ??
    (looksLikeQualityTrace(reportQuality) ? reportQuality : undefined)
  const supportMatrix = record(outputPreview.support_matrix) ?? record(manifestOutput?.support_matrix) ?? record(diagnostics.support_matrix)
  const verifiedFindings =
    record(outputPreview.verified_findings) ?? record(manifestOutput?.verified_findings) ?? record(diagnostics.verified_findings)
  const candidateClaims = firstList(outputPreview.candidate_claims, manifestOutput?.candidate_claims, diagnostics.candidate_claims)
  const lineageRefs = extractLineageRefs(input.lineage)
  const lineMaps = buildLineageMaps(lineageRefs)
  const qualityLineage = record(qualityTrace?.quality_lineage) ?? record(reportQuality.quality_lineage)
  const runId = input.runId ?? stringValue(runDetail?.run_id) ?? stringValue(runDetail?.id) ?? stringValue(runListItem?.run_id) ?? stringValue(runListItem?.id) ?? FALLBACK_RUN_ID
  const reportId =
    input.reportId ??
    stringValue(runDetail?.report_id) ??
    stringValue(manifest?.report_id) ??
    stringValue(manifestOutput?.report_id) ??
    stringValue(qualityLineage?.report_id) ??
    stringValue(reportQualityRoot?.report_id)

  const claims = new Map<string, StudioClaimEvidence>()

  for (const claim of list(qualityLineage?.claims)) {
    const claimRecord = record(claim)
    if (!claimRecord) continue
    const claimId = stableClaimId(claimRecord.claim_id ?? claimRecord.id, claimRecord.text ?? claimRecord.claim)
    const evidenceIds = uniqueStrings([
      ...stringList(claimRecord.supporting_evidence_ids),
      ...stringList(claimRecord.rejecting_evidence_ids),
      ...(lineMaps.evidenceIdsByClaim.get(claimId) ?? [])
    ])
    upsertClaim(claims, {
      claimId,
      claimText: stringValue(claimRecord.text) ?? stringValue(claimRecord.claim) ?? claimId,
      status: normalizeStatus(claimRecord.status, evidenceIds),
      sourceRefs: sourceRefsFromValues([
        ...stringList(claimRecord.supporting_sources),
        ...stringList(claimRecord.rejecting_sources),
        ...evidenceIds.flatMap((evidenceId) => lineMaps.sourceUrlsByEvidence.get(evidenceId) ?? [])
      ]),
      evidenceRefs: evidenceRefsFromIds(evidenceIds),
      failureReason: stringValue(claimRecord.rejection_reason) ?? stringValue(claimRecord.reason)
    })
  }

  for (const section of list(supportMatrix?.sections)) {
    const sectionRecord = record(section)
    if (!sectionRecord) continue
    const sectionTitle = stringValue(sectionRecord.section_title) ?? stringValue(sectionRecord.title) ?? stringValue(sectionRecord.section_id)
    const sectionSources = stringList(sectionRecord.cited_urls)
    for (const support of list(sectionRecord.claim_supports)) {
      const supportRecord = record(support)
      if (!supportRecord) continue
      const claimId = stableClaimId(supportRecord.claim_id, supportRecord.text ?? supportRecord.claim)
      const evidenceIds = uniqueStrings([...stringList(supportRecord.evidence_ids), ...(lineMaps.evidenceIdsByClaim.get(claimId) ?? [])])
      upsertClaim(claims, {
        claimId,
        claimText: stringValue(supportRecord.text) ?? stringValue(supportRecord.claim) ?? claimId,
        status: normalizeStatus(supportRecord.support_level ?? supportRecord.status, evidenceIds),
        confidence: numberValue(supportRecord.confidence),
        sourceRefs: sourceRefsFromValues([
          ...sectionSources,
          ...stringList(supportRecord.cited_urls),
          ...evidenceIds.flatMap((evidenceId) => lineMaps.sourceUrlsByEvidence.get(evidenceId) ?? [])
        ]),
        evidenceRefs: evidenceRefsFromIds(evidenceIds),
        reportSection: sectionTitle,
        failureReason: supportFailureReason(supportRecord)
      })
    }
  }

  addClaimList(claims, list(qualityTrace?.unsupported_claims), "unsupported")
  addClaimList(claims, list(qualityTrace?.rejected_claim_usage), "rejected")
  addClaimList(claims, list(supportMatrix?.unsupported_claims), "unsupported")
  addClaimList(claims, list(supportMatrix?.rejected_claim_usage), "rejected")
  addVerifiedFindings(claims, verifiedFindings, "accepted", "accepted_claims", lineMaps)
  addVerifiedFindings(claims, verifiedFindings, "rejected", "rejected_claims", lineMaps)
  addVerifiedFindings(claims, verifiedFindings, "uncertain", "uncertain_claims", lineMaps)

  for (const claim of candidateClaims) {
    const claimRecord = record(claim)
    if (!claimRecord) continue
    const claimId = stableClaimId(claimRecord.claim_id ?? claimRecord.id, claimRecord.text ?? claimRecord.claim)
    if (claims.has(claimId)) continue
    const evidenceIds = uniqueStrings([...stringList(claimRecord.source_evidence_ids), ...stringList(claimRecord.evidence_ids)])
    upsertClaim(claims, {
      claimId,
      claimText: stringValue(claimRecord.text) ?? stringValue(claimRecord.claim) ?? claimId,
      status: "uncertain",
      sourceRefs: sourceRefsFromValues([
        ...stringList(claimRecord.source_urls),
        ...evidenceIds.flatMap((evidenceId) => lineMaps.sourceUrlsByEvidence.get(evidenceId) ?? [])
      ]),
      evidenceRefs: evidenceRefsFromIds(evidenceIds),
      reportSection: stringValue(claimRecord.section_title) ?? stringValue(claimRecord.section_id),
      failureReason: "Awaiting quality decision"
    })
  }

  const claimRows = [...claims.values()].sort(compareClaims)
  const hasQualityTrace = Boolean(qualityTrace)
  const notices = uniqueStrings([
    ...(input.notices ?? []),
    ...(!hasQualityTrace ? ["This run did not include a quality_trace evidence matrix."] : []),
    ...(hasQualityTrace && claimRows.length === 0 ? ["No claim-level evidence rows were present in the quality trace."] : [])
  ])
  const dataState = input.dataState ?? (!hasQualityTrace ? "partial" : "ready")
  const llmTrace = adaptLlmTrace(record(outputPreview.llm_trace) ?? record(outputPreview.llmTrace) ?? record(diagnostics.llm_trace))

  return {
    runId,
    reportId,
    workflowName: stringValue(runDetail?.workflow_name) ?? stringValue(runDetail?.workflow_id) ?? stringValue(runListItem?.workflow_name) ?? stringValue(runListItem?.workflow_id),
    status: stringValue(runDetail?.status) ?? stringValue(runListItem?.status),
    startedAt: stringValue(runDetail?.started_at) ?? stringValue(runListItem?.started_at),
    finishedAt: stringValue(runDetail?.finished_at) ?? stringValue(runListItem?.finished_at),
    qualityScore: normalizeScore(numberValue(runDetail?.quality_score) ?? numberValue(runListItem?.quality_score) ?? numberValue(reportQualityRoot?.quality_score)),
    qualityDecision: stringValue(qualityTrace?.decision) ?? stringValue(reportQuality.decision),
    qualityRoute: stringValue(qualityTrace?.route) ?? stringValue(reportQuality.route),
    counts: countClaims(claimRows),
    citationFailureCategories: normalizeFailureCategories(qualityTrace?.citation_failure_categories ?? reportQuality.citation_failure_categories),
    unsupportedSections: uniqueStrings([...stringList(qualityTrace?.unsupported_sections), ...stringList(supportMatrix?.unsupported_sections)]),
    hasQualityTrace,
    dataState,
    notices,
    claims: claimRows,
    qualityLineage,
    llmTrace,
    lineageRefs
  }
}

export function summarizeRunEvidence(detail: StudioRunEvidenceDetail): StudioEvidenceRunSummary {
  return {
    runId: detail.runId,
    reportId: detail.reportId,
    workflowName: detail.workflowName,
    status: detail.status,
    startedAt: detail.startedAt,
    finishedAt: detail.finishedAt,
    qualityScore: detail.qualityScore,
    qualityDecision: detail.qualityDecision,
    qualityRoute: detail.qualityRoute,
    counts: detail.counts,
    citationFailureCategories: detail.citationFailureCategories,
    unsupportedSections: detail.unsupportedSections,
    hasQualityTrace: detail.hasQualityTrace,
    dataState: detail.dataState,
    notices: detail.notices
  }
}

export function adaptEvidenceOverview(details: StudioRunEvidenceDetail[], notices: string[] = []): StudioEvidenceOverview {
  const runs = details.map(summarizeRunEvidence)
  const totals = runs.reduce(
    (current, run) => ({
      accepted: current.accepted + run.counts.accepted,
      rejected: current.rejected + run.counts.rejected,
      uncertain: current.uncertain + run.counts.uncertain,
      unsupported: current.unsupported + run.counts.unsupported,
      total: current.total + run.counts.total
    }),
    emptyCounts()
  )
  return {
    runs,
    totals,
    citationFailureCategories: mergeFailureCategories(runs.flatMap((run) => run.citationFailureCategories)),
    dataState: overviewDataState(runs),
    notices: uniqueStrings([...notices, ...runs.flatMap((run) => run.notices)]),
    generatedAt: new Date().toISOString()
  }
}

export function buildFallbackRunEvidenceDetail(runId = FALLBACK_RUN_ID, notices: string[] = []): StudioRunEvidenceDetail {
  const claims: StudioClaimEvidence[] = [
    {
      claimId: "claim-runtime-trace",
      claimText: "Runtime trace data supports the report's main operational finding.",
      status: "accepted",
      confidence: 0.92,
      sourceRefs: [{ sourceId: "source-official-ai", title: "Official AI Platform Blog", url: "https://example.com/platform-trace", reliability: "high" }],
      evidenceRefs: [{ evidenceId: "ev-trace-1", quote: "Trace exports include step and decision metadata." }],
      reportSection: "Runtime Signals"
    },
    {
      claimId: "claim-market-size",
      claimText: "A market-size claim lacks a primary source.",
      status: "unsupported",
      confidence: 0.28,
      sourceRefs: [],
      evidenceRefs: [],
      reportSection: "Business Impact",
      failureReason: "No supporting evidence id was attached to the claim."
    },
    {
      claimId: "claim-rejected-vendor",
      claimText: "A rejected vendor acquisition claim was still referenced.",
      status: "rejected",
      confidence: 0.18,
      sourceRefs: [{ url: "https://example.com/retraction", reliability: "low" }],
      evidenceRefs: [{ evidenceId: "ev-reject-1", summary: "The source contradicts the acquisition statement." }],
      reportSection: "Vendor Movement",
      failureReason: "Rejected claim usage detected by citation checks."
    },
    {
      claimId: "claim-open-question",
      claimText: "The rollout timing remains uncertain until a second source confirms it.",
      status: "uncertain",
      confidence: 0.51,
      sourceRefs: [{ url: "https://example.com/roadmap", reliability: "medium" }],
      evidenceRefs: [{ evidenceId: "ev-uncertain-1", summary: "Single-source roadmap mention." }],
      reportSection: "Release Window",
      failureReason: "Only one supporting source is available."
    }
  ]

  return {
    runId,
    reportId: `${runId}:final`,
    workflowName: "fallback-evidence-workflow",
    status: "review_required",
    startedAt: "2026-05-22T00:00:00.000Z",
    qualityScore: 72,
    qualityDecision: "human_review",
    qualityRoute: "fallback",
    counts: countClaims(claims),
    citationFailureCategories: [
      { code: "unsupported_claims", count: 1, items: ["A market-size claim lacks a primary source."] },
      { code: "rejected_claim_usage", count: 1, items: ["A rejected vendor acquisition claim was still referenced."] }
    ],
    unsupportedSections: ["Business Impact"],
    hasQualityTrace: true,
    dataState: "fallback",
    notices: uniqueStrings(["Evidence API unavailable; showing deterministic fallback evidence chain.", ...notices]),
    claims,
    qualityLineage: {
      report_id: `${runId}:final`,
      claim_count: claims.length,
      supporting_evidence_ids: ["ev-trace-1", "ev-uncertain-1"],
      rejecting_evidence_ids: ["ev-reject-1"]
    },
    llmTrace: {
      selectedDeploymentId: "fallback",
      fallbackUsed: true,
      fallbackCount: 1,
      routerEventCount: 0,
      sanitized: { selected_deployment_id: "fallback", fallback_used: true, router_event_count: 0 }
    },
    lineageRefs: []
  }
}

export function buildFallbackEvidenceOverview(notices: string[] = []): StudioEvidenceOverview {
  return adaptEvidenceOverview([buildFallbackRunEvidenceDetail(FALLBACK_RUN_ID, notices)], notices)
}

export function filterClaimsByStatus(claims: StudioClaimEvidence[], status: ClaimSupportStatus | "all"): StudioClaimEvidence[] {
  if (status === "all") return claims
  return claims.filter((claim) => claim.status === status)
}

export function countClaims(claims: StudioClaimEvidence[]): StudioEvidenceCounts {
  return claims.reduce(
    (counts, claim) => {
      counts[claim.status] += 1
      counts.total += 1
      return counts
    },
    emptyCounts()
  )
}

export function adaptLlmTrace(rawTrace: Record<string, unknown> | undefined): StudioLlmTrace | undefined {
  if (!rawTrace) return undefined
  const sanitized = sanitizeRecord(rawTrace)
  return {
    selectedDeploymentId: stringValue(rawTrace.selected_deployment_id) ?? stringValue(rawTrace.selectedDeploymentId),
    fallbackUsed: booleanValue(rawTrace.fallback_used) ?? booleanValue(rawTrace.fallbackUsed),
    fallbackCount: numberValue(rawTrace.fallback_count) ?? numberValue(rawTrace.fallbackCount),
    providerErrorCount: numberValue(rawTrace.provider_error_count) ?? numberValue(rawTrace.providerErrorCount),
    cooldownSkipCount: numberValue(rawTrace.cooldown_skip_count) ?? numberValue(rawTrace.cooldownSkipCount),
    routerEventCount: numberValue(rawTrace.router_event_count) ?? numberValue(rawTrace.routerEventCount),
    budgetCheck: sanitized.budget_check ?? sanitized.budgetCheck,
    globalBudgetCheck: sanitized.global_budget_check ?? sanitized.globalBudgetCheck,
    sanitized
  }
}

function addVerifiedFindings(
  claims: Map<string, StudioClaimEvidence>,
  verifiedFindings: Record<string, unknown> | undefined,
  status: ClaimSupportStatus,
  key: string,
  lineMaps: ReturnType<typeof buildLineageMaps>
) {
  for (const claim of list(verifiedFindings?.[key])) {
    const claimRecord = record(claim)
    if (!claimRecord) continue
    const claimId = stableClaimId(claimRecord.claim_id ?? claimRecord.id, claimRecord.claim ?? claimRecord.text)
    const evidenceIds = uniqueStrings([
      ...stringList(claimRecord.supporting_evidence_ids),
      ...stringList(claimRecord.rejecting_evidence_ids),
      ...stringList(claimRecord.evidence_ids),
      ...(lineMaps.evidenceIdsByClaim.get(claimId) ?? [])
    ])
    upsertClaim(claims, {
      claimId,
      claimText: stringValue(claimRecord.claim) ?? stringValue(claimRecord.text) ?? claimId,
      status,
      sourceRefs: sourceRefsFromValues([
        ...stringList(claimRecord.supporting_sources),
        ...stringList(claimRecord.rejecting_sources),
        ...stringList(claimRecord.source_urls),
        ...evidenceIds.flatMap((evidenceId) => lineMaps.sourceUrlsByEvidence.get(evidenceId) ?? [])
      ]),
      evidenceRefs: evidenceRefsFromIds(evidenceIds),
      reportSection: stringValue(claimRecord.section_title) ?? stringValue(claimRecord.section_id),
      failureReason: stringValue(claimRecord.rejection_reason) ?? stringValue(claimRecord.notes)
    })
  }
}

function addClaimList(
  claims: Map<string, StudioClaimEvidence>,
  rawClaims: unknown[],
  status: ClaimSupportStatus
) {
  for (const item of rawClaims) {
    const claim = claimFromRiskItem(item, status)
    if (claim) upsertClaim(claims, claim)
  }
}

function claimFromRiskItem(item: unknown, status: ClaimSupportStatus): StudioClaimEvidence | undefined {
  if (typeof item === "string") {
    const { section, text } = splitSectionPrefix(item)
    return {
      claimId: stableClaimId(undefined, item),
      claimText: text || item,
      status,
      sourceRefs: [],
      evidenceRefs: [],
      reportSection: section,
      failureReason: status === "rejected" ? "Rejected claim usage" : "Unsupported claim"
    }
  }
  const value = record(item)
  if (!value) return undefined
  const text = stringValue(value.text) ?? stringValue(value.claim) ?? stringValue(value.message) ?? stringValue(value.reason) ?? "Evidence claim"
  return {
    claimId: stableClaimId(value.claim_id ?? value.id, text),
    claimText: text,
    status,
    sourceRefs: sourceRefsFromValues([...stringList(value.sources), ...stringList(value.source_urls), ...stringList(value.supporting_sources)]),
    evidenceRefs: evidenceRefsFromIds([...stringList(value.evidence_ids), ...stringList(value.supporting_evidence_ids), ...stringList(value.rejecting_evidence_ids)]),
    reportSection: stringValue(value.section_title) ?? stringValue(value.section_id),
    failureReason: stringValue(value.reason) ?? stringValue(value.severity) ?? (status === "rejected" ? "Rejected claim usage" : "Unsupported claim")
  }
}

function upsertClaim(claims: Map<string, StudioClaimEvidence>, next: StudioClaimEvidence) {
  const existing = claims.get(next.claimId)
  if (!existing) {
    claims.set(next.claimId, normalizeClaim(next))
    return
  }
  claims.set(next.claimId, normalizeClaim({
    ...existing,
    ...next,
    status: STATUS_WEIGHT[next.status] >= STATUS_WEIGHT[existing.status] ? next.status : existing.status,
    confidence: next.confidence ?? existing.confidence,
    sourceRefs: mergeSourceRefs(existing.sourceRefs, next.sourceRefs),
    evidenceRefs: mergeEvidenceRefs(existing.evidenceRefs, next.evidenceRefs),
    reportSection: next.reportSection ?? existing.reportSection,
    failureReason: next.failureReason ?? existing.failureReason
  }))
}

function normalizeClaim(claim: StudioClaimEvidence): StudioClaimEvidence {
  return {
    ...claim,
    claimText: claim.claimText || claim.claimId,
    sourceRefs: mergeSourceRefs([], claim.sourceRefs),
    evidenceRefs: mergeEvidenceRefs([], claim.evidenceRefs)
  }
}

function normalizeStatus(value: unknown, evidenceIds: string[] = []): ClaimSupportStatus {
  const status = String(value ?? "").trim().toLowerCase()
  if (status === "accepted" || status === "passed" || status === "supported" || status === "supports" || status === "success") return "accepted"
  if (status === "rejected" || status === "failed" || status === "contradicted") return "rejected"
  if (status === "uncertain" || status === "unknown" || status === "needs_review" || status === "review_required") return "uncertain"
  if (status === "unsupported" || status === "missing" || status === "unverified") return "unsupported"
  return evidenceIds.length ? "accepted" : "uncertain"
}

function normalizeFailureCategories(value: unknown): StudioCitationFailureCategory[] {
  return list(value).map((item) => {
    if (typeof item === "string") return { code: item, count: 1, items: [] }
    const itemRecord = record(item) ?? {}
    const items = stringList(itemRecord.items)
    return {
      code: stringValue(itemRecord.code) ?? stringValue(itemRecord.name) ?? "citation_failure",
      count: numberValue(itemRecord.count) ?? items.length,
      items,
      label: stringValue(itemRecord.label)
    }
  })
}

function mergeFailureCategories(categories: StudioCitationFailureCategory[]): StudioCitationFailureCategory[] {
  const byCode = new Map<string, StudioCitationFailureCategory>()
  for (const category of categories) {
    const existing = byCode.get(category.code)
    if (!existing) {
      byCode.set(category.code, { ...category, items: uniqueStrings(category.items) })
      continue
    }
    byCode.set(category.code, {
      ...existing,
      count: existing.count + category.count,
      items: uniqueStrings([...existing.items, ...category.items])
    })
  }
  return [...byCode.values()].sort((left, right) => right.count - left.count || left.code.localeCompare(right.code))
}

function overviewDataState(runs: StudioEvidenceRunSummary[]): StudioEvidenceDataState {
  if (!runs.length) return "fallback"
  if (runs.every((run) => run.dataState === "fallback")) return "fallback"
  if (runs.some((run) => run.dataState !== "ready")) return "partial"
  return "ready"
}

function buildLineageMaps(lineageRefs: Array<Record<string, unknown>>) {
  const sourceUrlsByEvidence = new Map<string, string[]>()
  const evidenceIdsByClaim = new Map<string, string[]>()
  for (const ref of lineageRefs) {
    const sourceType = stringValue(ref.source_type) ?? stringValue(ref.sourceType)
    const sourceId = stringValue(ref.source_id) ?? stringValue(ref.sourceId)
    const targetType = stringValue(ref.target_type) ?? stringValue(ref.targetType)
    const targetId = stringValue(ref.target_id) ?? stringValue(ref.targetId)
    if (sourceType === "source_url" && sourceId && targetType === "evidence" && targetId) {
      sourceUrlsByEvidence.set(targetId, uniqueStrings([...(sourceUrlsByEvidence.get(targetId) ?? []), sourceId]))
    }
    if (sourceType === "evidence" && sourceId && targetType === "claim" && targetId) {
      evidenceIdsByClaim.set(targetId, uniqueStrings([...(evidenceIdsByClaim.get(targetId) ?? []), sourceId]))
    }
  }
  return { sourceUrlsByEvidence, evidenceIdsByClaim }
}

function extractLineageRefs(lineage: unknown): Array<Record<string, unknown>> {
  const root = record(lineage)
  const refs = list(root?.lineage_refs ?? root?.lineageRefs)
  return refs.map(record).filter((item): item is Record<string, unknown> => Boolean(item))
}

function evidenceRefsFromIds(values: string[]): StudioEvidenceRef[] {
  return uniqueStrings(values).map((value) => ({ evidenceId: value }))
}

function sourceRefsFromValues(values: string[]): StudioEvidenceSourceRef[] {
  return uniqueStrings(values).map((value) => isLikelyUrl(value) ? { url: value } : { sourceId: value, title: value })
}

function mergeSourceRefs(left: StudioEvidenceSourceRef[], right: StudioEvidenceSourceRef[]): StudioEvidenceSourceRef[] {
  const byKey = new Map<string, StudioEvidenceSourceRef>()
  for (const source of [...left, ...right]) {
    const key = source.url ?? source.sourceId ?? source.title
    if (!key) continue
    byKey.set(key, { ...byKey.get(key), ...source })
  }
  return [...byKey.values()]
}

function mergeEvidenceRefs(left: StudioEvidenceRef[], right: StudioEvidenceRef[]): StudioEvidenceRef[] {
  const byKey = new Map<string, StudioEvidenceRef>()
  for (const evidence of [...left, ...right]) {
    const key = evidence.evidenceId ?? evidence.quote ?? evidence.summary
    if (!key) continue
    byKey.set(key, { ...byKey.get(key), ...evidence })
  }
  return [...byKey.values()]
}

function supportFailureReason(value: Record<string, unknown>): string | undefined {
  return stringValue(value.failure_reason) ?? stringValue(value.reason) ?? stringValue(value.support_type) ?? stringValue(value.severity)
}

function stableClaimId(value: unknown, textValue: unknown): string {
  const id = stringValue(value)
  if (id) return id
  const text = stringValue(textValue) ?? "claim"
  const hash = Array.from(text).reduce((current, char) => (current * 31 + char.charCodeAt(0)) >>> 0, 0).toString(16)
  return `claim-${hash}`
}

function splitSectionPrefix(value: string): { section?: string; text: string } {
  const index = value.indexOf(":")
  if (index <= 0) return { text: value }
  const section = value.slice(0, index).trim()
  const text = value.slice(index + 1).trim()
  return { section, text }
}

function compareClaims(left: StudioClaimEvidence, right: StudioClaimEvidence): number {
  return STATUS_WEIGHT[right.status] - STATUS_WEIGHT[left.status] || left.claimId.localeCompare(right.claimId)
}

function emptyCounts(): StudioEvidenceCounts {
  return { accepted: 0, rejected: 0, uncertain: 0, unsupported: 0, total: 0 }
}

function looksLikeQualityTrace(value: Record<string, unknown> | undefined): value is Record<string, unknown> {
  if (!value) return false
  return "decision" in value || "quality_lineage" in value || "citation_failure_categories" in value || "unsupported_claims" in value
}

function sanitizeRecord(value: Record<string, unknown>): Record<string, unknown> {
  const sanitized: Record<string, unknown> = {}
  for (const [key, item] of Object.entries(value)) {
    sanitized[key] = isSensitiveKey(key) ? "[redacted]" : sanitizeValue(item)
  }
  return sanitized
}

function sanitizeValue(value: unknown): unknown {
  if (typeof value === "string") return isSensitiveValue(value) ? "[redacted]" : value
  if (Array.isArray(value)) return value.slice(0, 50).map(sanitizeValue)
  const valueRecord = record(value)
  if (valueRecord) return sanitizeRecord(valueRecord)
  return value
}

function isSensitiveKey(key: string): boolean {
  return /token|api[_-]?key|secret|password|authorization|credential|prompt|raw[_-]?response/i.test(key)
}

function isSensitiveValue(value: string): boolean {
  return /bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|api[_-]?key\s*[:=]/i.test(value)
}

function firstList(...values: unknown[]): unknown[] {
  for (const value of values) {
    const items = list(value)
    if (items.length) return items
  }
  return []
}

function list(value: unknown): unknown[] {
  if (Array.isArray(value)) return value
  if (typeof value === "string" && value) return [value]
  return []
}

function stringList(value: unknown): string[] {
  if (typeof value === "string") return value ? [value] : []
  if (Array.isArray(value)) return value.map((item) => stringValue(item)).filter((item): item is string => Boolean(item))
  return []
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.length ? value : undefined
}

function numberValue(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value)
  return undefined
}

function booleanValue(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined
}

function normalizeScore(value: number | undefined): number | undefined {
  if (value === undefined) return undefined
  return value <= 1 ? Math.round(value * 100) : Math.round(value)
}

function uniqueStrings(values: string[]): string[] {
  const seen = new Set<string>()
  const result: string[] = []
  for (const value of values) {
    const trimmed = value.trim()
    if (!trimmed || seen.has(trimmed)) continue
    seen.add(trimmed)
    result.push(trimmed)
  }
  return result
}

function isLikelyUrl(value: string): boolean {
  return /^https?:\/\//i.test(value)
}
