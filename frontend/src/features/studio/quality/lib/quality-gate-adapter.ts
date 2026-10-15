import type {
  StudioBlockedRun,
  StudioQualityCheck,
  StudioQualityDashboard,
  StudioQualityDataState,
  StudioQualityDetail,
  StudioQualityMetrics,
  StudioQualityReportSummary,
  StudioQualityStatus
} from "@/types/quality"

export type ApiReportList = {
  report_count?: number
  reports?: ApiReportSummary[]
}

export type ApiReportSummary = {
  report_id?: string | null
  id?: string | null
  run_id?: string | null
  title?: string | null
  status?: string | null
  quality_score?: number | null
  finished_at?: string | null
  generated_at?: string | null
  created_at?: string | null
  workflow_id?: string | null
  manifest_path?: string | null
  artifact_path?: string | null
}

export type ApiReportQuality = {
  report_id?: string | null
  run_id?: string | null
  status?: string | null
  quality_score?: number | null
  quality?: Record<string, unknown> | null
}

export type ApiRunHealth = {
  run_id?: string | null
  health?: Record<string, unknown> | null
}

export type ApiRunDiagnostics = {
  run_id?: string | null
  diagnostics?: Record<string, unknown> | null
}

export type ApiCatalogHealth = {
  health?: Record<string, unknown> | null
}

export type QualityApiErrorNotice = {
  source: string
  message: string
  requestId?: string
}

export const fallbackQualityDashboard: StudioQualityDashboard = {
  dataState: "fallback",
  notices: ["API unavailable. Showing deterministic Quality Gate fallback data; Request Review requires live API data."],
  counts: {
    passed: 2,
    warning: 1,
    failed: 1,
    review_required: 1,
    unknown: 0
  },
  metrics: {
    citationCoverage: 74,
    sourceFreshness: 86,
    duplicateRate: 9,
    unsupportedClaims: 3
  },
  recentFailedReports: [
    {
      reportId: "fallback-report-blocked",
      runId: "fallback-run-quality",
      title: "Fallback Daily Intelligence Gate",
      status: "review_required",
      reportStatus: "final",
      qualityScore: 64,
      generatedAt: "2026-05-22T08:54:00.000Z",
      workflowId: "daily-intelligence-agentic",
      artifactPath: "report_quality_summary.json",
      issueCount: 3,
      failureReasons: ["Unsupported claims require editor review", "Citation coverage is below publish threshold"]
    }
  ],
  recentBlockedRuns: [
    {
      runId: "fallback-run-quality",
      status: "review_required",
      severity: "warning",
      summary: "Quality gate blocked publishing until citation gaps are reviewed.",
      latestEventCount: 18
    }
  ],
  reports: [
    {
      reportId: "fallback-report-passed",
      runId: "fallback-run-passed",
      title: "Fallback Passed Intelligence Report",
      status: "passed",
      reportStatus: "final",
      qualityScore: 91,
      generatedAt: "2026-05-22T07:30:00.000Z",
      workflowId: "daily-intelligence-agentic",
      artifactPath: "report.json",
      issueCount: 0,
      failureReasons: []
    },
    {
      reportId: "fallback-report-warning",
      runId: "fallback-run-warning",
      title: "Fallback Source Freshness Warning",
      status: "warning",
      reportStatus: "final",
      qualityScore: 78,
      generatedAt: "2026-05-22T06:30:00.000Z",
      workflowId: "daily-intelligence-agentic",
      artifactPath: "report.json",
      issueCount: 1,
      failureReasons: ["Source freshness has degraded for one community source"]
    }
  ]
}

export const fallbackQualityDetail: StudioQualityDetail = {
  dataState: "fallback",
  notices: ["API unavailable. Showing deterministic report quality fallback; Request Review is disabled."],
  report: fallbackQualityDashboard.recentFailedReports[0],
  checks: [
    {
      id: "fallback-citation-coverage",
      name: "Citation coverage",
      status: "failed",
      score: 74,
      message: "High-impact claims need stronger source support.",
      userActionRequired: true
    },
    {
      id: "fallback-source-freshness",
      name: "Source freshness",
      status: "warning",
      score: 86,
      message: "Most sources are fresh; one community source is stale."
    },
    {
      id: "fallback-unsupported-claims",
      name: "Unsupported claims",
      status: "review_required",
      score: 0,
      message: "Three claims require human review before publish.",
      userActionRequired: true
    },
    {
      id: "fallback-duplicate-rate",
      name: "Duplicate rate",
      status: "passed",
      score: 91,
      message: "Duplicate risk is inside the gate threshold."
    }
  ],
  failureReasons: ["Unsupported claims require editor review", "Citation coverage is below publish threshold"],
  metrics: fallbackQualityDashboard.metrics,
  run: {
    runId: "fallback-run-quality",
    health: { severity: "warning", healthy: false },
    diagnostics: { healthy: false, summary: "fallback diagnostics" }
  },
  artifactRefs: [{ label: "report_quality_summary", value: "report_quality_summary.json" }],
  rawQuality: {
    decision: "human_review",
    route: "human_review"
  }
}

export function buildQualityDashboard(input: {
  reports?: ApiReportList
  qualities?: ApiReportQuality[]
  catalogHealth?: ApiCatalogHealth
  runHealth?: ApiRunHealth[]
  diagnostics?: ApiRunDiagnostics[]
  errors?: QualityApiErrorNotice[]
}): StudioQualityDashboard {
  const apiReports = input.reports?.reports ?? []
  const qualityByReportId = new Map(
    (input.qualities ?? [])
      .map((quality) => [quality.report_id ?? undefined, quality] as const)
      .filter((item): item is readonly [string, ApiReportQuality] => Boolean(item[0]))
  )

  if (!apiReports.length && !input.qualities?.length) {
    return {
      ...fallbackQualityDashboard,
      notices: [...noticeFromErrors(input.errors), ...fallbackQualityDashboard.notices],
      requestId: firstRequestId(input.errors)
    }
  }

  const reports = apiReports.map((report) => {
    const reportIdValue = reportId(report)
    const quality = reportIdValue ? qualityByReportId.get(reportIdValue) : undefined
    return mapReportSummary(report, quality)
  })

  const qualityOnlyReports = (input.qualities ?? [])
    .filter((quality) => quality.report_id && !reports.some((report) => report.reportId === quality.report_id))
    .map((quality) => mapReportSummary({ report_id: quality.report_id, run_id: quality.run_id, status: quality.status, quality_score: quality.quality_score }, quality))

  const mergedReports = [...reports, ...qualityOnlyReports]
  const counts = countStatuses(mergedReports)
  const metrics = aggregateMetrics(input.qualities ?? [])
  const blockedRuns = mapBlockedRuns(input.runHealth ?? [], input.diagnostics ?? [])
  const dataState = dashboardState(input.errors, mergedReports.length)

  return {
    dataState,
    notices: noticesForState(dataState, input.errors),
    requestId: firstRequestId(input.errors),
    counts,
    metrics,
    recentFailedReports: mergedReports
      .filter((report) => report.status === "failed" || report.status === "review_required" || report.status === "warning")
      .slice(0, 6),
    recentBlockedRuns: blockedRuns,
    reports: mergedReports,
    catalogHealth: objectOrUndefined(input.catalogHealth?.health)
  }
}

export function buildQualityDetail(input: {
  reportId: string
  report?: ApiReportSummary
  quality?: ApiReportQuality
  runHealth?: ApiRunHealth
  diagnostics?: ApiRunDiagnostics
  errors?: QualityApiErrorNotice[]
}): StudioQualityDetail {
  if (!input.report && !input.quality) {
    return {
      ...fallbackQualityDetail,
      notices: [...noticeFromErrors(input.errors), ...fallbackQualityDetail.notices],
      requestId: firstRequestId(input.errors),
      report: {
        ...fallbackQualityDetail.report,
        reportId: input.reportId
      }
    }
  }

  const report = mapReportSummary(
    input.report ?? {
      report_id: input.quality?.report_id ?? input.reportId,
      run_id: input.quality?.run_id,
      status: input.quality?.status,
      quality_score: input.quality?.quality_score
    },
    input.quality
  )
  const checks = mapQualityChecks(input.quality)
  const metrics = metricsFromQuality(input.quality)
  const dataState = detailState(input.errors, input.quality)
  const rawQuality = objectOrUndefined(input.quality?.quality)

  return {
    dataState,
    notices: noticesForState(dataState, input.errors),
    requestId: firstRequestId(input.errors),
    report,
    checks,
    failureReasons: failureReasons(input.quality),
    metrics,
    run: report.runId
      ? {
          runId: report.runId,
          health: objectOrUndefined(input.runHealth?.health),
          diagnostics: objectOrUndefined(input.diagnostics?.diagnostics)
        }
      : undefined,
    artifactRefs: artifactRefs(rawQuality),
    rawQuality
  }
}

export function mapQualityChecks(input?: ApiReportQuality): StudioQualityCheck[] {
  const quality = objectOrUndefined(input?.quality)
  if (!quality) {
    return [
      {
        id: "quality-unknown",
        name: "Report quality",
        status: "unknown",
        message: "No structured quality payload is available for this report."
      }
    ]
  }

  const lineage = objectOrUndefined(quality.quality_lineage)
  const citationCategories = arrayValue(quality.citation_failure_categories)
  const unsupportedClaims = arrayValue(quality.unsupported_claims)
  const unsupportedSections = arrayValue(quality.unsupported_sections)
  const reviewerTrace = objectOrUndefined(quality.reviewer_trace)
  const checks = arrayValue(quality.checks)

  const mappedChecks = checks
    .map((item, index) => mapRawCheck(item, index))
    .filter((item): item is StudioQualityCheck => Boolean(item))

  const generatedChecks: StudioQualityCheck[] = [
    {
      id: "quality-decision",
      name: "Gate decision",
      status: statusFromDecision(readString(quality.decision) ?? input?.status, input?.quality_score),
      score: normalizeScore(input?.quality_score ?? numberValue(quality.quality_score) ?? numberValue(quality.score)),
      message: decisionMessage(quality)
    },
    {
      id: "citation-coverage",
      name: "Citation coverage",
      status: statusFromScore(metricValue(quality, ["citation_coverage", "citation_coverage_score", "coverage"]), {
        warning: 80,
        failed: 60
      }),
      score: metricValue(quality, ["citation_coverage", "citation_coverage_score", "coverage"]),
      message: citationCategories.length
        ? `${citationCategories.length} citation failure categories detected.`
        : "Citation coverage is available for gate inspection.",
      details: citationCategories.length ? { categories: citationCategories } : undefined,
      userActionRequired: citationCategories.length > 0
    },
    {
      id: "source-freshness",
      name: "Source freshness",
      status: statusFromScore(metricValue(quality, ["source_freshness", "source_freshness_score", "freshness"]), {
        warning: 75,
        failed: 50
      }),
      score: metricValue(quality, ["source_freshness", "source_freshness_score", "freshness"]),
      message: "Source freshness is evaluated against the reporting window."
    },
    {
      id: "duplicate-rate",
      name: "Duplicate rate",
      status: statusFromRate(metricValue(quality, ["duplicate_rate", "duplicateRisk", "duplicate_risk"]), {
        warning: 15,
        failed: 30
      }),
      score: metricValue(quality, ["duplicate_rate", "duplicateRisk", "duplicate_risk"]),
      message: "Duplicate rate is kept visible as a publish risk signal."
    },
    {
      id: "unsupported-claims",
      name: "Unsupported claims",
      status: unsupportedClaims.length || unsupportedSections.length ? "review_required" : "passed",
      score: unsupportedClaims.length + unsupportedSections.length,
      message:
        unsupportedClaims.length || unsupportedSections.length
          ? `${unsupportedClaims.length + unsupportedSections.length} unsupported claim or section signals need review.`
          : "No unsupported claims were reported by the quality gate.",
      details: { unsupportedClaims, unsupportedSections },
      userActionRequired: unsupportedClaims.length > 0 || unsupportedSections.length > 0
    },
    {
      id: "quality-lineage",
      name: "Quality lineage",
      status: lineageStatus(lineage),
      score: numberValue(lineage?.claim_count),
      message: lineage
        ? `${numberValue(lineage.claim_count) ?? 0} claims and ${numberValue(lineage.quality_result_count) ?? 0} quality records linked.`
        : "No lineage summary is attached to this quality payload.",
      details: lineage
    },
    {
      id: "reviewer-trace",
      name: "Reviewer trace",
      status: reviewerTrace ? "review_required" : "unknown",
      message: reviewerTrace
        ? `Reviewer trace is present${readString(reviewerTrace.approval_id) ? ` for approval ${readString(reviewerTrace.approval_id)}` : ""}.`
        : "Reviewer trace has not been created yet.",
      details: reviewerTrace,
      userActionRequired: Boolean(reviewerTrace)
    }
  ]

  return mergeChecks([...mappedChecks, ...generatedChecks])
}

export function statusFromDecision(decision: string | undefined | null, score?: number | null): StudioQualityStatus {
  const normalized = decision?.toLowerCase()
  if (normalized === "passed" || normalized === "pass" || normalized === "approved" || normalized === "published" || normalized === "final") {
    return scoreStatus(score)
  }
  if (
    normalized === "warning" ||
    normalized === "warn" ||
    normalized === "degraded" ||
    normalized === "needs_changes"
  ) {
    return "warning"
  }
  if (
    normalized === "review" ||
    normalized === "review_required" ||
    normalized === "human_review" ||
    normalized === "approval_required" ||
    normalized === "blocked"
  ) {
    return "review_required"
  }
  if (normalized === "failed" || normalized === "fail" || normalized === "rejected" || normalized === "error") {
    return "failed"
  }
  return scoreStatus(score)
}

function mapReportSummary(report: ApiReportSummary, quality?: ApiReportQuality): StudioQualityReportSummary {
  const qualityPayload = objectOrUndefined(quality?.quality)
  const status = statusFromDecision(
    readString(qualityPayload?.decision) ?? readString(qualityPayload?.route) ?? quality?.status ?? report.status,
    quality?.quality_score ?? report.quality_score
  )
  const checks = mapQualityChecks(quality)

  return {
    reportId: reportId(report) ?? quality?.report_id ?? "unknown-report",
    runId: report.run_id ?? quality?.run_id ?? undefined,
    title: report.title ?? reportId(report) ?? quality?.report_id ?? "Untitled report",
    status,
    reportStatus: report.status ?? quality?.status ?? undefined,
    qualityScore: normalizeScore(quality?.quality_score ?? report.quality_score ?? numberValue(qualityPayload?.quality_score)),
    generatedAt: report.finished_at ?? report.generated_at ?? report.created_at ?? undefined,
    workflowId: report.workflow_id ?? undefined,
    artifactPath: report.artifact_path ?? report.manifest_path ?? firstArtifactRef(qualityPayload),
    issueCount: checks.filter((check) => check.status === "failed" || check.status === "warning" || check.status === "review_required").length,
    failureReasons: failureReasons(quality)
  }
}

function mapBlockedRuns(runHealth: ApiRunHealth[], diagnostics: ApiRunDiagnostics[]): StudioBlockedRun[] {
  const diagnosticsByRun = new Map(
    diagnostics
      .map((item) => [item.run_id ?? undefined, item] as const)
      .filter((item): item is readonly [string, ApiRunDiagnostics] => Boolean(item[0]))
  )

  return runHealth
    .map((item): StudioBlockedRun | undefined => {
      const health = objectOrUndefined(item.health)
      const severity = readString(health?.severity) ?? readString(health?.status)
      const status = statusFromDecision(severity, undefined)
      const diagnosticsForRun = item.run_id ? diagnosticsByRun.get(item.run_id) : undefined
      const diag = objectOrUndefined(diagnosticsForRun?.diagnostics)
      const healthy = booleanValue(health?.healthy) ?? booleanValue(diag?.healthy)
      if (healthy === true && status !== "failed" && status !== "warning" && status !== "review_required") return undefined
      return {
        runId: item.run_id ?? diagnosticsForRun?.run_id ?? "unknown-run",
        status: healthy === false && status === "unknown" ? "review_required" : status,
        severity,
        summary: readString(health?.summary) ?? readString(diag?.summary) ?? "Run health requires operator inspection.",
        latestEventCount: numberValue(objectOrUndefined(diag?.timeline_summary)?.event_count)
      }
    })
    .filter((item): item is StudioBlockedRun => Boolean(item))
    .slice(0, 6)
}

function aggregateMetrics(qualities: ApiReportQuality[]): StudioQualityMetrics {
  const metrics = qualities.map(metricsFromQuality)
  return {
    citationCoverage: average(metrics.map((item) => item.citationCoverage)),
    sourceFreshness: average(metrics.map((item) => item.sourceFreshness)),
    duplicateRate: average(metrics.map((item) => item.duplicateRate)),
    unsupportedClaims: metrics.reduce((total, item) => total + item.unsupportedClaims, 0)
  }
}

function metricsFromQuality(input?: ApiReportQuality): StudioQualityMetrics {
  const quality = objectOrUndefined(input?.quality)
  return {
    citationCoverage: metricValue(quality, ["citation_coverage", "citation_coverage_score", "coverage"]),
    sourceFreshness: metricValue(quality, ["source_freshness", "source_freshness_score", "freshness"]),
    duplicateRate: metricValue(quality, ["duplicate_rate", "duplicateRisk", "duplicate_risk"]),
    unsupportedClaims:
      arrayValue(quality?.unsupported_claims).length +
      arrayValue(quality?.unsupported_sections).length +
      arrayValue(quality?.rejected_claim_usage).length
  }
}

function countStatuses(reports: StudioQualityReportSummary[]): Record<StudioQualityStatus, number> {
  const counts: Record<StudioQualityStatus, number> = {
    passed: 0,
    warning: 0,
    failed: 0,
    review_required: 0,
    unknown: 0
  }
  reports.forEach((report) => {
    counts[report.status] += 1
  })
  return counts
}

function failureReasons(input?: ApiReportQuality): string[] {
  const quality = objectOrUndefined(input?.quality)
  if (!quality) return []
  const reasons = [
    ...arrayValue(quality.remediation).map(stringifyBrief),
    ...arrayValue(quality.unsupported_sections).map((item) => `Unsupported section: ${stringifyBrief(item)}`),
    ...arrayValue(quality.unsupported_claims).map((item) => `Unsupported claim: ${stringifyBrief(item)}`),
    ...arrayValue(quality.citation_failure_categories).map((item) => {
      const record = objectOrUndefined(item)
      return record?.code ? `Citation failure: ${String(record.code)}` : `Citation failure: ${stringifyBrief(item)}`
    })
  ].filter(Boolean)

  if (!reasons.length && statusFromDecision(readString(quality.decision), input?.quality_score) !== "passed") {
    const decision = readString(quality.decision) ?? readString(quality.route)
    return decision ? [`Gate decision: ${decision}`] : []
  }
  return [...new Set(reasons)].slice(0, 8)
}

function artifactRefs(quality: Record<string, unknown> | undefined): Array<{ label: string; value: string }> {
  if (!quality) return []
  const refs = objectOrUndefined(objectOrUndefined(quality.reviewer_trace)?.artifact_refs) ?? objectOrUndefined(quality.artifact_refs)
  if (!refs) return []
  return Object.entries(refs)
    .filter(([, value]) => typeof value === "string" && value.length > 0)
    .map(([label, value]) => ({ label, value: String(value) }))
}

function firstArtifactRef(quality: Record<string, unknown> | undefined): string | undefined {
  return artifactRefs(quality)[0]?.value
}

function mapRawCheck(input: unknown, index: number): StudioQualityCheck | undefined {
  const record = objectOrUndefined(input)
  if (!record) return undefined
  return {
    id: readString(record.id) ?? readString(record.check_id) ?? `api-check-${index + 1}`,
    name: readString(record.name) ?? readString(record.label) ?? readString(record.check_name) ?? `Check ${index + 1}`,
    status: statusFromDecision(readString(record.status) ?? readString(record.decision), numberValue(record.score)),
    score: normalizeScore(numberValue(record.score)),
    message: readString(record.message) ?? readString(record.summary) ?? readString(record.reason),
    details: record,
    userActionRequired: booleanValue(record.userActionRequired) ?? booleanValue(record.user_action_required)
  }
}

function mergeChecks(checks: StudioQualityCheck[]): StudioQualityCheck[] {
  const seen = new Set<string>()
  return checks.filter((check) => {
    if (seen.has(check.id)) return false
    seen.add(check.id)
    return true
  })
}

function scoreStatus(score?: number | null): StudioQualityStatus {
  const normalized = normalizeScore(score)
  if (normalized === undefined) return "unknown"
  if (normalized >= 85) return "passed"
  if (normalized >= 70) return "warning"
  return "failed"
}

function statusFromScore(score: number | undefined, thresholds: { warning: number; failed: number }): StudioQualityStatus {
  if (score === undefined) return "unknown"
  if (score < thresholds.failed) return "failed"
  if (score < thresholds.warning) return "warning"
  return "passed"
}

function statusFromRate(rate: number | undefined, thresholds: { warning: number; failed: number }): StudioQualityStatus {
  if (rate === undefined) return "unknown"
  if (rate >= thresholds.failed) return "failed"
  if (rate >= thresholds.warning) return "warning"
  return "passed"
}

function lineageStatus(lineage: Record<string, unknown> | undefined): StudioQualityStatus {
  if (!lineage) return "unknown"
  const rejected = numberValue(lineage.rejected_claim_count) ?? arrayValue(lineage.rejecting_evidence_ids).length
  return rejected > 0 ? "warning" : "passed"
}

function decisionMessage(quality: Record<string, unknown>): string {
  const decision = readString(quality.decision) ?? "unknown"
  const route = readString(quality.route)
  return route ? `Quality gate decision is ${decision}; route is ${route}.` : `Quality gate decision is ${decision}.`
}

function metricValue(quality: Record<string, unknown> | undefined, keys: string[]): number | undefined {
  if (!quality) return undefined
  for (const key of keys) {
    const direct = numberValue(quality[key])
    if (direct !== undefined) return normalizeScore(direct)
    const metrics = objectOrUndefined(quality.metrics)
    const nested = numberValue(metrics?.[key])
    if (nested !== undefined) return normalizeScore(nested)
  }
  return undefined
}

function normalizeScore(value: number | null | undefined): number | undefined {
  if (value === null || value === undefined || Number.isNaN(value)) return undefined
  return value <= 1 ? Math.round(value * 100) : Math.round(value)
}

function average(values: Array<number | undefined>): number | undefined {
  const filtered = values.filter((value): value is number => value !== undefined)
  if (!filtered.length) return undefined
  return Math.round(filtered.reduce((total, value) => total + value, 0) / filtered.length)
}

function reportId(report: ApiReportSummary): string | undefined {
  return report.report_id ?? report.id ?? undefined
}

function dashboardState(errors: QualityApiErrorNotice[] | undefined, itemCount: number): StudioQualityDataState {
  if (!itemCount) return "fallback"
  return errors?.length ? "partial" : "ready"
}

function detailState(errors: QualityApiErrorNotice[] | undefined, quality: ApiReportQuality | undefined): StudioQualityDataState {
  if (!quality) return errors?.length ? "partial" : "fallback"
  return errors?.length ? "partial" : "ready"
}

function noticesForState(state: StudioQualityDataState, errors?: QualityApiErrorNotice[]): string[] {
  const notices = noticeFromErrors(errors)
  if (state === "ready") return notices
  if (state === "partial") return [...notices, "Some Quality Gate API calls failed; missing fields are filled from available gate evidence."]
  return [...notices, "API unavailable. Showing deterministic Quality Gate fallback data; Request Review requires live API data."]
}

function noticeFromErrors(errors?: QualityApiErrorNotice[]): string[] {
  return (errors ?? []).map((error) => {
    const requestId = error.requestId ? ` RequestId: ${error.requestId}.` : ""
    return `${error.source} API failed: ${error.message}.${requestId}`
  })
}

function firstRequestId(errors?: QualityApiErrorNotice[]): string | undefined {
  return errors?.find((error) => error.requestId)?.requestId
}

function objectOrUndefined(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : undefined
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function readString(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined
}

function booleanValue(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined
}

function stringifyBrief(value: unknown): string {
  if (typeof value === "string") return value
  if (value === undefined || value === null) return ""
  try {
    return JSON.stringify(value).slice(0, 180)
  } catch {
    return String(value)
  }
}
