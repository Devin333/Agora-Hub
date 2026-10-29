import { safeApiGet, type SafeApiResult } from "@/lib/api/server"
import { apiBaseUrl } from "@/lib/deployment-profile"
import {
  buildQualityDashboard,
  buildQualityDetail,
  fallbackQualityDashboard,
  fallbackQualityDetail,
  type ApiCatalogHealth,
  type ApiReportList,
  type ApiReportQuality,
  type ApiReportSummary,
  type ApiRunDiagnostics,
  type ApiRunHealth,
  type QualityApiErrorNotice
} from "@/features/studio/quality/lib/quality-gate-adapter"
import type {
  StudioQualityDashboard,
  StudioQualityDetail,
  StudioRequestReviewPayload,
  StudioRequestReviewResult
} from "@/types/quality"

type ApiEnvelope<T> = {
  success?: boolean
  data?: T | null
  error?: {
    code?: string
    message?: string
    detail?: unknown
    details?: unknown
    requestId?: string | null
    request_id?: string | null
  } | null
  request_id?: string | null
}

type RequestReviewApiResponse = {
  message?: string
  approval_id?: string
  approval?: {
    approval_id?: string
  } | null
}

type SourcedResult<T> = SafeApiResult<T> & { source: string }


export async function getQualityDashboard(): Promise<StudioQualityDashboard> {
  const reportsResult = await safeApiGet<ApiReportList>("/api/v1/reports?limit=25")

  if (!reportsResult.ok || !reportsResult.data.reports?.length) {
    return {
      ...fallbackQualityDashboard,
      notices: [
        apiNotice("reports", reportsResult),
        ...fallbackQualityDashboard.notices
      ].filter(Boolean) as string[],
      requestId: reportsResult.ok ? undefined : reportsResult.requestId
    }
  }

  const reports = reportsResult.data.reports
  const qualityResults = await Promise.all(
    reports.slice(0, 12).map(async (report) => {
      const id = reportId(report)
      if (!id) {
        return {
          ok: false as const,
          errorCode: "missing_report_id",
          errorMessage: "report_id missing",
          source: "report quality"
        }
      }
      const result = await safeApiGet<ApiReportQuality>(`/api/v1/reports/${encodeURIComponent(id)}/quality`)
      return { ...result, source: `report quality ${id}` }
    })
  )

  const qualities = qualityResults.filter(isOkResult).map((result) => result.data)
  const qualityErrors = qualityResults.filter(isFailedResult).map((result) => ({
    source: result.source,
    message: result.errorMessage,
    requestId: result.requestId
  }))

  const runIds = unique(
    reports
      .map((report) => report.run_id)
      .filter((id): id is string => Boolean(id))
      .slice(0, 8)
  )

  const [catalogHealthResult, runHealthResults, diagnosticsResults] = await Promise.all([
    safeApiGet<ApiCatalogHealth>("/api/v1/runs/catalog/health"),
    Promise.all(
      runIds.map(async (runIdValue) => ({
        ...(await safeApiGet<ApiRunHealth>(`/api/v1/runs/${encodeURIComponent(runIdValue)}/health`)),
        source: `run health ${runIdValue}`
      }))
    ),
    Promise.all(
      runIds.map(async (runIdValue) => ({
        ...(await safeApiGet<ApiRunDiagnostics>(`/api/v1/runs/${encodeURIComponent(runIdValue)}/diagnostics`)),
        source: `run diagnostics ${runIdValue}`
      }))
    )
  ])

  const errors: QualityApiErrorNotice[] = [
    ...qualityErrors,
    ...noticeFromResult("catalog health", catalogHealthResult),
    ...runHealthResults.filter(isFailedResult).map((result) => ({
      source: result.source,
      message: result.errorMessage,
      requestId: result.requestId
    })),
    ...diagnosticsResults.filter(isFailedResult).map((result) => ({
      source: result.source,
      message: result.errorMessage,
      requestId: result.requestId
    }))
  ]

  return buildQualityDashboard({
    reports: reportsResult.data,
    qualities,
    catalogHealth: catalogHealthResult.ok ? catalogHealthResult.data : undefined,
    runHealth: runHealthResults.filter(isOkResult).map((result) => result.data),
    diagnostics: diagnosticsResults.filter(isOkResult).map((result) => result.data),
    errors
  })
}

export async function getQualityDetail(reportIdValue: string): Promise<StudioQualityDetail> {
  const decodedReportId = decodeURIComponent(reportIdValue)
  const reportsResult = await safeApiGet<ApiReportList>("/api/v1/reports?limit=50")
  const report = reportsResult.ok
    ? reportsResult.data.reports?.find((item) => reportId(item) === decodedReportId)
    : undefined
  const qualityResult = await safeApiGet<ApiReportQuality>(`/api/v1/reports/${encodeURIComponent(decodedReportId)}/quality`)

  if (!qualityResult.ok && !report) {
    return {
      ...fallbackQualityDetail,
      report: {
        ...fallbackQualityDetail.report,
        reportId: decodedReportId
      },
      notices: [
        ...noticeFromResult("reports", reportsResult).map(formatNotice),
        formatNotice({
          source: "report quality",
          message: qualityResult.errorMessage,
          requestId: qualityResult.requestId
        }),
        ...fallbackQualityDetail.notices
      ],
      requestId: qualityResult.requestId ?? (reportsResult.ok ? undefined : reportsResult.requestId)
    }
  }

  const runIdValue = report?.run_id ?? (qualityResult.ok ? qualityResult.data.run_id : undefined)
  const [runHealthResult, diagnosticsResult] = runIdValue
    ? await Promise.all([
        safeApiGet<ApiRunHealth>(`/api/v1/runs/${encodeURIComponent(runIdValue)}/health`),
        safeApiGet<ApiRunDiagnostics>(`/api/v1/runs/${encodeURIComponent(runIdValue)}/diagnostics`)
      ])
    : [undefined, undefined]

  const errors: QualityApiErrorNotice[] = [
    ...noticeFromResult("reports", reportsResult),
    ...noticeFromResult("report quality", qualityResult),
    ...(runHealthResult ? noticeFromResult("run health", runHealthResult) : []),
    ...(diagnosticsResult ? noticeFromResult("run diagnostics", diagnosticsResult) : [])
  ]

  return buildQualityDetail({
    reportId: decodedReportId,
    report,
    quality: qualityResult.ok ? qualityResult.data : undefined,
    runHealth: runHealthResult?.ok ? runHealthResult.data : undefined,
    diagnostics: diagnosticsResult?.ok ? diagnosticsResult.data : undefined,
    errors
  })
}

export async function requestReportReview(
  reportId: string,
  payload: StudioRequestReviewPayload
): Promise<StudioRequestReviewResult> {
  if (!payload.reason.trim()) {
    return { ok: false, errorMessage: "Reason is required." }
  }

  try {
    const response = await fetch(apiUrl(`/api/v1/reports/${encodeURIComponent(reportId)}/request-review`), {
      method: "POST",
      headers: requestHeaders(),
      cache: "no-store",
      body: JSON.stringify(payload)
    })
    const requestId = response.headers.get("x-request-id") ?? undefined
    const parsed = await parseResponse<RequestReviewApiResponse>(response)

    if (!response.ok || !parsed.ok) {
      const errorMessage = parsed.ok ? response.statusText : parsed.errorMessage
      return {
        ok: false,
        errorMessage: errorMessage || "Request review failed.",
        requestId: parsed.requestId ?? requestId
      }
    }

    return {
      ok: true,
      approvalId: parsed.data.approval?.approval_id ?? parsed.data.approval_id,
      message: parsed.data.message ?? "Report review requested.",
      requestId: parsed.requestId ?? requestId
    }
  } catch (error) {
    return {
      ok: false,
      errorMessage: error instanceof Error ? error.message : "Request review failed."
    }
  }
}

function reportId(report: ApiReportSummary): string | undefined {
  return report.report_id ?? report.id ?? undefined
}

function isOkResult<T>(result: SourcedResult<T>): result is { ok: true; data: T; source: string } {
  return result.ok
}

function isFailedResult<T>(result: SourcedResult<T>): result is { ok: false; errorCode: string; errorMessage: string; requestId?: string; source: string } {
  return !result.ok
}

function noticeFromResult<T>(source: string, result: SafeApiResult<T>): QualityApiErrorNotice[] {
  if (result.ok) return []
  return [{ source, message: result.errorMessage, requestId: result.requestId }]
}

function apiNotice<T>(source: string, result: SafeApiResult<T>): string | undefined {
  if (result.ok) return undefined
  return formatNotice({ source, message: result.errorMessage, requestId: result.requestId })
}

function formatNotice(notice: QualityApiErrorNotice): string {
  return `${notice.source} API failed: ${notice.message}.${notice.requestId ? ` RequestId: ${notice.requestId}.` : ""}`
}

function unique(values: string[]): string[] {
  return [...new Set(values)]
}

async function parseResponse<T>(
  response: Response
): Promise<{ ok: true; data: T; requestId?: string } | { ok: false; errorMessage: string; requestId?: string }> {
  const requestId = response.headers.get("x-request-id") ?? undefined
  const contentType = response.headers.get("content-type") ?? ""
  const payload = contentType.includes("application/json") ? await response.json() : await response.text()

  if (isEnvelope<T>(payload)) {
    if (payload.success === false) {
      return {
        ok: false,
        errorMessage: payload.error?.message ?? "API request failed.",
        requestId: payload.error?.requestId ?? payload.error?.request_id ?? payload.request_id ?? requestId
      }
    }
    return { ok: true, data: (payload.data ?? {}) as T, requestId: payload.request_id ?? requestId }
  }

  if (!response.ok) {
    return {
      ok: false,
      errorMessage:
        typeof payload === "object" && payload && "message" in payload ? String(payload.message) : response.statusText,
      requestId
    }
  }

  return { ok: true, data: payload as T, requestId }
}

function isEnvelope<T>(payload: unknown): payload is ApiEnvelope<T> {
  return typeof payload === "object" && payload !== null && "success" in payload
}

function apiUrl(path: string): string {
  if (path.startsWith("http://") || path.startsWith("https://")) return path
  const suffix = path.startsWith("/") ? path : `/${path}`
  return `${apiBaseUrl()}${suffix}`
}

function requestHeaders(): HeadersInit {
  const headers: Record<string, string> = {
    Accept: "application/json",
    "Content-Type": "application/json"
  }
  const apiToken = process.env.NEWSROOM_API_TOKEN ?? process.env.NEWS_API_TOKEN
  if (apiToken) headers.Authorization = `Bearer ${apiToken}`
  return headers
}
