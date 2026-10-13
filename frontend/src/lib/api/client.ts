import { normalizeApiError } from "@/lib/api/api-errors"
import { unwrapApiEnvelope } from "@/lib/api/api-envelope"
import { resolveMockPath } from "@/lib/api/mock-data"
import type { StudioApiError } from "@/types/studio"

const DEFAULT_API_BASE_URL = ""

export class ApiRequestError extends Error {
  code: string
  detail?: unknown
  requestId?: string
  status?: number
  retryable?: boolean
  userActionRequired?: boolean

  constructor(error: StudioApiError, status?: number) {
    super(error.message)
    this.name = "ApiRequestError"
    this.code = error.code
    this.detail = error.details
    this.requestId = error.requestId
    this.status = status ?? error.status
    this.retryable = error.retryable
    this.userActionRequired = error.userActionRequired
  }
}

export async function apiGet<T>(path: string, init?: RequestInit): Promise<T> {
  if (shouldUseMock()) {
    const mock = resolveMockPath(path)
    if (mock !== undefined) return mock as T
  }

  const response = await fetch(apiUrl(path), {
    ...init,
    method: "GET",
    headers: {
      Accept: "application/json",
      ...init?.headers
    }
  })

  return parseJsonResponse<T>(response)
}

export async function apiPost<T>(path: string, body?: unknown, init?: RequestInit): Promise<T> {
  if (shouldUseMock()) {
    const mock = resolveMockPath(path)
    if (mock !== undefined) return mock as T
  }

  const response = await fetch(apiUrl(path), {
    ...init,
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...init?.headers
    },
    body: body === undefined ? undefined : JSON.stringify(body)
  })

  return parseJsonResponse<T>(response)
}

export async function apiPatch<T>(path: string, body?: unknown, init?: RequestInit): Promise<T> {
  if (shouldUseMock()) {
    const mock = resolveMockPath(path)
    if (mock !== undefined) return mock as T
  }

  const response = await fetch(apiUrl(path), {
    ...init,
    method: "PATCH",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...init?.headers
    },
    body: body === undefined ? undefined : JSON.stringify(body)
  })

  return parseJsonResponse<T>(response)
}

export async function apiDelete<T>(path: string, init?: RequestInit): Promise<T> {
  if (shouldUseMock()) {
    const mock = resolveMockPath(path)
    if (mock !== undefined) return mock as T
  }

  const response = await fetch(apiUrl(path), {
    ...init,
    method: "DELETE",
    headers: {
      Accept: "application/json",
      ...init?.headers
    }
  })

  return parseJsonResponse<T>(response)
}

async function parseJsonResponse<T>(response: Response): Promise<T> {
  const requestId = response.headers.get("x-request-id") ?? undefined
  const contentType = response.headers.get("content-type") ?? ""
  const payload = contentType.includes("application/json") ? await response.json() : await response.text()

  if (!response.ok) {
    const normalized = normalizeApiError(payload)
    throw new ApiRequestError(
      {
        ...normalized,
        code: normalized.code === "request_failed" ? `http_${response.status}` : normalized.code,
        message: normalized.message || response.statusText || "API request failed",
        details: normalized.details ?? payload,
        requestId: normalized.requestId ?? requestId,
        status: response.status
      },
      response.status
    )
  }

  const result = unwrapApiEnvelope<unknown>(payload)
  if (!result.ok) {
    throw new ApiRequestError(
      {
        ...result.error,
        requestId: result.error.requestId ?? requestId
      },
      response.status
    )
  }

  return payload as T
}

function apiUrl(path: string): string {
  if (path.startsWith("http://") || path.startsWith("https://")) return path
  const suffix = path.startsWith("/") ? path : `/${path}`
  if (suffix.startsWith("/api/")) return suffix
  const baseUrl = process.env.NEXT_PUBLIC_NEWSROOM_API_BASE_URL ?? DEFAULT_API_BASE_URL
  if (!baseUrl) return suffix
  return `${baseUrl.replace(/\/$/, "")}${suffix}`
}

export function shouldUseMock(): boolean {
  return (
    process.env.NEXT_PUBLIC_NEWSROOM_USE_MOCKS === "true" &&
    process.env.NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE !== "local-desktop"
  )
}
