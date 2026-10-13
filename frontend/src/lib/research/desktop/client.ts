import {
  desktopCapabilitiesSchema,
  desktopIdentitySchema,
  operationSchema,
  parseDesktopReader,
  researchReportListSchema,
  researchReportSchema,
  savedResearchReportSchema,
  type DesktopCapabilities,
  type DesktopIdentity,
  type DesktopReaderPayload,
  type ResearchOperation,
  type ResearchReport,
  type ResearchReportReference,
  type SavedResearchReport,
} from "./contracts"

const MAX_REPORT_EXPORT_BYTES = 20 * 1024 * 1024

type Envelope<T> = {
  success?: boolean
  data?: T
  error?: {
    code?: string
    message?: string
    requestId?: string
  }
}

export class DesktopResearchError extends Error {
  constructor(
    public code: string,
    message: string,
    public status?: number,
    public requestId?: string,
  ) {
    super(message)
    this.name = "DesktopResearchError"
  }
}

export async function fetchDesktopIdentity(signal?: AbortSignal): Promise<DesktopIdentity> {
  return desktopIdentitySchema.parse(
    await request("/api/research/desktop/identity", { signal }),
  )
}

export async function fetchDesktopCapabilities(
  signal?: AbortSignal,
): Promise<DesktopCapabilities> {
  return desktopCapabilitiesSchema.parse(
    await request("/api/research/desktop/capabilities", { signal }),
  )
}

export async function fetchDesktopReader(
  paperId: string,
  signal?: AbortSignal,
): Promise<DesktopReaderPayload> {
  return parseDesktopReader(
    await request(
      `/api/research/desktop/papers/${encodeURIComponent(paperId)}/reader`,
      { signal },
    ),
  )
}

export async function submitResearchPrepare(
  body: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<ResearchOperation> {
  return operationSchema.parse(
    await request(
      "/api/research/desktop/operations/prepare",
      jsonPost(body, signal),
    ),
  )
}

export async function submitResearchAnalyze(
  body: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<ResearchOperation> {
  return operationSchema.parse(
    await request(
      "/api/research/desktop/operations/analyze",
      jsonPost(body, signal),
    ),
  )
}

export async function submitResearchAsk(
  body: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<ResearchOperation> {
  return operationSchema.parse(
    await request("/api/research/desktop/operations/ask", jsonPost(body, signal)),
  )
}

export async function fetchResearchOperation(
  requestId: string,
  generation: string,
  signal?: AbortSignal,
): Promise<ResearchOperation> {
  const query = new URLSearchParams({ generation })
  return operationSchema.parse(
    await request(
      `/api/research/desktop/operations/${encodeURIComponent(requestId)}?${query}`,
      { signal },
    ),
  )
}

export async function fetchSavedResearchReports(
  signal?: AbortSignal,
): Promise<ResearchReportReference[]> {
  return researchReportListSchema.parse(
    await request("/api/research/desktop/reports", { signal }),
  ).reports
}

export async function fetchSavedResearchReport(
  reportId: string,
  signal?: AbortSignal,
): Promise<SavedResearchReport> {
  return savedResearchReportSchema.parse(
    await request(
      `/api/research/desktop/reports/${encodeURIComponent(reportId)}`,
      { signal },
    ),
  )
}

export async function fetchResearchReport(
  runId: string,
  sourceRevision: string,
  signal?: AbortSignal,
): Promise<ResearchReport> {
  const query = new URLSearchParams({ sourceRevision })
  return researchReportSchema.parse(
    await request(
      `/api/research/desktop/reports/runs/${encodeURIComponent(runId)}?${query}`,
      { signal },
    ),
  )
}

export async function saveResearchReport(
  body: {
    reportId: string
    runId: string
    sourceRevision: string
    title: string
  },
  signal?: AbortSignal,
): Promise<SavedResearchReport> {
  return savedResearchReportSchema.parse(
    await request(
      "/api/research/desktop/reports",
      jsonPost(body, signal),
    ),
  )
}

export async function downloadResearchReport(
  runId: string,
  sourceRevision: string,
  format: "markdown" | "json",
  signal?: AbortSignal,
): Promise<{ blob: Blob; filename: string }> {
  const query = new URLSearchParams({ sourceRevision, format })
  const response = await fetch(
    `/api/research/desktop/reports/runs/${encodeURIComponent(runId)}/export?${query}`,
    {
      signal,
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: format === "json" ? "application/json" : "text/markdown" },
    },
  )
  if (!response.ok) throw await responseError(response, "报告导出失败。")

  const declared = Number(response.headers.get("content-length") ?? "0")
  if (Number.isFinite(declared) && declared > MAX_REPORT_EXPORT_BYTES) {
    throw new DesktopResearchError("report_export_too_large", "报告导出内容超过大小限制。")
  }
  const blob = await response.blob()
  if (blob.size > MAX_REPORT_EXPORT_BYTES) {
    throw new DesktopResearchError("report_export_too_large", "报告导出内容超过大小限制。")
  }
  return {
    blob,
    filename:
      contentDispositionFilename(response.headers.get("content-disposition")) ??
      `research-report-${safeFilenamePart(runId)}.${format === "markdown" ? "md" : "json"}`,
  }
}

export async function exportResearchDraft(
  body: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<Response> {
  return fetch("/api/research/desktop/reports/drafts/export", {
    ...jsonPost(body, signal),
    credentials: "same-origin",
    cache: "no-store",
  })
}

async function request(path: string, init: RequestInit): Promise<unknown> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    cache: "no-store",
    headers: { Accept: "application/json", ...init.headers },
  })
  const payload = (await response.json().catch(() => ({}))) as Envelope<unknown>
  if (!response.ok || payload.success === false) {
    throw new DesktopResearchError(
      payload.error?.code ?? `http_${response.status}`,
      payload.error?.message ?? "研究服务返回错误。",
      response.status,
      payload.error?.requestId,
    )
  }
  return payload.success === true ? payload.data : payload
}

async function responseError(response: Response, fallback: string): Promise<DesktopResearchError> {
  const payload = (await response.json().catch(() => ({}))) as Envelope<unknown>
  return new DesktopResearchError(
    payload.error?.code ?? `http_${response.status}`,
    payload.error?.message ?? fallback,
    response.status,
    payload.error?.requestId,
  )
}

function jsonPost(body: Record<string, unknown>, signal?: AbortSignal): RequestInit {
  return {
    method: "POST",
    signal,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }
}

function contentDispositionFilename(value: string | null): string | null {
  if (!value) return null
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(value)?.[1]
  const plain = /filename="?([^";]+)"?/i.exec(value)?.[1]
  const candidate = encoded ? decodeURIComponent(encoded) : plain
  if (!candidate) return null
  const safe = candidate.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "-").trim()
  return safe ? safe.slice(0, 180) : null
}

function safeFilenamePart(value: string): string {
  return value.replace(/[^A-Za-z0-9._-]/g, "-").slice(0, 100) || "report"
}
