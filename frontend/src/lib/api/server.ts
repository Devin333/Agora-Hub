import type { ApiError } from "@/types/common"

const DEFAULT_API_BASE_URL = "http://localhost:8000"

type ApiEnvelope<T> = {
  success?: boolean
  data?: T | null
  error?: (ApiError & { details?: unknown; request_id?: string | null }) | null
  request_id?: string | null
}

export type SafeApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; errorCode: string; errorMessage: string; requestId?: string; retryAfter?: number; status?: number }

export class NewsRoomApiError extends Error {
  code: string
  detail?: unknown
  requestId?: string
  status?: number

  constructor(error: ApiError, status?: number) {
    super(error.message)
    this.name = "NewsRoomApiError"
    this.code = error.code
    this.detail = error.detail
    this.requestId = error.requestId
    this.status = status
  }
}

export async function safeApiGet<T>(path: string, init?: RequestInit): Promise<SafeApiResult<T>> {
  try {
    return { ok: true, data: await apiGet<T>(path, init) }
  } catch (error) {
    if (error instanceof NewsRoomApiError) {
      return {
        ok: false,
        errorCode: error.code,
        errorMessage: error.message,
        requestId: error.requestId,
        status: error.status
      }
    }
    return {
      ok: false,
      errorCode: "request_failed",
      errorMessage: error instanceof Error ? error.message : "Request failed"
    }
  }
}

export async function safeApiPost<T>(path: string, body?: unknown, init?: RequestInit): Promise<SafeApiResult<T>> {
  try {
    return { ok: true, data: await apiPost<T>(path, body, init) }
  } catch (error) {
    if (error instanceof NewsRoomApiError) {
      return {
        ok: false,
        errorCode: error.code,
        errorMessage: error.message,
        requestId: error.requestId,
        status: error.status,
        ...(retryAfterSeconds(error.detail) !== undefined ? { retryAfter: retryAfterSeconds(error.detail) } : {})
      }
    }
    return {
      ok: false,
      errorCode: "request_failed",
      errorMessage: error instanceof Error ? error.message : "Request failed"
    }
  }
}

export async function safeApiPut<T>(path: string, body?: unknown, init?: RequestInit): Promise<SafeApiResult<T>> {
  try {
    return { ok: true, data: await apiPut<T>(path, body, init) }
  } catch (error) {
    if (error instanceof NewsRoomApiError) {
      return {
        ok: false,
        errorCode: error.code,
        errorMessage: error.message,
        requestId: error.requestId,
        status: error.status
      }
    }
    return {
      ok: false,
      errorCode: "request_failed",
      errorMessage: error instanceof Error ? error.message : "Request failed"
    }
  }
}

function retryAfterSeconds(detail: unknown): number | undefined {
  const value = detail && typeof detail === "object" && "retryAfter" in detail ? detail.retryAfter : undefined
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.ceil(value) : undefined
}

export async function safeApiPatch<T>(path: string, body?: unknown, init?: RequestInit): Promise<SafeApiResult<T>> {
  try {
    return { ok: true, data: await apiPatch<T>(path, body, init) }
  } catch (error) {
    if (error instanceof NewsRoomApiError) {
      return {
        ok: false,
        errorCode: error.code,
        errorMessage: error.message,
        requestId: error.requestId,
        status: error.status
      }
    }
    return {
      ok: false,
      errorCode: "request_failed",
      errorMessage: error instanceof Error ? error.message : "Request failed"
    }
  }
}

export async function safeApiDelete<T>(path: string, init?: RequestInit): Promise<SafeApiResult<T>> {
  try {
    return { ok: true, data: await apiDelete<T>(path, init) }
  } catch (error) {
    if (error instanceof NewsRoomApiError) {
      return {
        ok: false,
        errorCode: error.code,
        errorMessage: error.message,
        requestId: error.requestId,
        status: error.status
      }
    }
    return {
      ok: false,
      errorCode: "request_failed",
      errorMessage: error instanceof Error ? error.message : "Request failed"
    }
  }
}

async function apiGet<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(apiUrl(path), {
    ...init,
    method: "GET",
    headers: requestHeaders(init?.headers),
    cache: "no-store"
  })

  const payload = await parsePayload<T>(response)
  if (!response.ok) {
    if (isApiEnvelope<T>(payload) && payload.error) {
      throw new NewsRoomApiError(
        {
          code: payload.error.code,
          message: payload.error.message,
          detail: "detail" in payload.error ? payload.error.detail : payload.error.details,
          requestId: payload.error.requestId ?? payload.error.request_id ?? payload.request_id ?? undefined
        },
        response.status
      )
    }
    throw new NewsRoomApiError(
      {
        code: `http_${response.status}`,
        message: response.statusText || "API request failed",
        detail: payload
      },
      response.status
    )
  }

  if (isApiEnvelope<T>(payload)) {
    if (payload.success === false) {
      const error = payload.error ?? {
        code: "api_error",
        message: "API request failed",
        detail: payload
      }
      throw new NewsRoomApiError(
        {
          code: error.code,
          message: error.message,
          detail: "detail" in error ? error.detail : error.details,
          requestId: error.requestId ?? error.request_id ?? payload.request_id ?? undefined
        },
        response.status
      )
    }
    return payload.data as T
  }

  return payload as T
}

async function apiPost<T>(path: string, body?: unknown, init?: RequestInit): Promise<T> {
  const response = await fetch(apiUrl(path), {
    ...init,
    method: "POST",
    headers: {
      ...requestHeaders(init?.headers),
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store"
  })

  const payload = await parsePayload<T>(response)
  if (!response.ok) {
    if (isApiEnvelope<T>(payload) && payload.error) {
      throw new NewsRoomApiError(
        {
          code: payload.error.code,
          message: payload.error.message,
          detail: "detail" in payload.error ? payload.error.detail : payload.error.details,
          requestId: payload.error.requestId ?? payload.error.request_id ?? payload.request_id ?? undefined
        },
        response.status
      )
    }
    throw new NewsRoomApiError(
      {
        code: `http_${response.status}`,
        message: response.statusText || "API request failed",
        detail: payload
      },
      response.status
    )
  }

  if (isApiEnvelope<T>(payload)) {
    if (payload.success === false) {
      const error = payload.error ?? {
        code: "api_error",
        message: "API request failed",
        detail: payload
      }
      throw new NewsRoomApiError(
        {
          code: error.code,
          message: error.message,
          detail: "detail" in error ? error.detail : error.details,
          requestId: error.requestId ?? error.request_id ?? payload.request_id ?? undefined
        },
        response.status
      )
    }
    return payload.data as T
  }

  return payload as T
}

async function apiPut<T>(path: string, body?: unknown, init?: RequestInit): Promise<T> {
  const response = await fetch(apiUrl(path), {
    ...init,
    method: "PUT",
    headers: {
      ...requestHeaders(init?.headers),
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store"
  })

  const payload = await parsePayload<T>(response)
  if (!response.ok) {
    if (isApiEnvelope<T>(payload) && payload.error) {
      throw new NewsRoomApiError(
        {
          code: payload.error.code,
          message: payload.error.message,
          detail: "detail" in payload.error ? payload.error.detail : payload.error.details,
          requestId: payload.error.requestId ?? payload.error.request_id ?? payload.request_id ?? undefined
        },
        response.status
      )
    }
    throw new NewsRoomApiError(
      {
        code: `http_${response.status}`,
        message: response.statusText || "API request failed",
        detail: payload
      },
      response.status
    )
  }

  if (isApiEnvelope<T>(payload)) {
    if (payload.success === false) {
      const error = payload.error ?? {
        code: "api_error",
        message: "API request failed",
        detail: payload
      }
      throw new NewsRoomApiError(
        {
          code: error.code,
          message: error.message,
          detail: "detail" in error ? error.detail : error.details,
          requestId: error.requestId ?? error.request_id ?? payload.request_id ?? undefined
        },
        response.status
      )
    }
    return payload.data as T
  }

  return payload as T
}

async function apiPatch<T>(path: string, body?: unknown, init?: RequestInit): Promise<T> {
  const response = await fetch(apiUrl(path), {
    ...init,
    method: "PATCH",
    headers: {
      ...requestHeaders(init?.headers),
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store"
  })

  const payload = await parsePayload<T>(response)
  if (!response.ok) {
    if (isApiEnvelope<T>(payload) && payload.error) {
      throw new NewsRoomApiError(
        {
          code: payload.error.code,
          message: payload.error.message,
          detail: "detail" in payload.error ? payload.error.detail : payload.error.details,
          requestId: payload.error.requestId ?? payload.error.request_id ?? payload.request_id ?? undefined
        },
        response.status
      )
    }
    throw new NewsRoomApiError(
      {
        code: `http_${response.status}`,
        message: response.statusText || "API request failed",
        detail: payload
      },
      response.status
    )
  }

  if (isApiEnvelope<T>(payload)) {
    if (payload.success === false) {
      const error = payload.error ?? {
        code: "api_error",
        message: "API request failed",
        detail: payload
      }
      throw new NewsRoomApiError(
        {
          code: error.code,
          message: error.message,
          detail: "detail" in error ? error.detail : error.details,
          requestId: error.requestId ?? error.request_id ?? payload.request_id ?? undefined
        },
        response.status
      )
    }
    return payload.data as T
  }

  return payload as T
}

async function apiDelete<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(apiUrl(path), {
    ...init,
    method: "DELETE",
    headers: requestHeaders(init?.headers),
    cache: "no-store"
  })

  const payload = await parsePayload<T>(response)
  if (!response.ok) {
    if (isApiEnvelope<T>(payload) && payload.error) {
      throw new NewsRoomApiError(
        {
          code: payload.error.code,
          message: payload.error.message,
          detail: "detail" in payload.error ? payload.error.detail : payload.error.details,
          requestId: payload.error.requestId ?? payload.error.request_id ?? payload.request_id ?? undefined
        },
        response.status
      )
    }
    throw new NewsRoomApiError(
      {
        code: `http_${response.status}`,
        message: response.statusText || "API request failed",
        detail: payload
      },
      response.status
    )
  }

  if (isApiEnvelope<T>(payload)) {
    if (payload.success === false) {
      const error = payload.error ?? {
        code: "api_error",
        message: "API request failed",
        detail: payload
      }
      throw new NewsRoomApiError(
        {
          code: error.code,
          message: error.message,
          detail: "detail" in error ? error.detail : error.details,
          requestId: error.requestId ?? error.request_id ?? payload.request_id ?? undefined
        },
        response.status
      )
    }
    return payload.data as T
  }

  return payload as T
}

async function parsePayload<T>(response: Response): Promise<T | ApiEnvelope<T> | string> {
  const contentType = response.headers.get("content-type") ?? ""
  if (contentType.includes("application/json")) {
    return (await response.json()) as T | ApiEnvelope<T>
  }
  return response.text()
}

function isApiEnvelope<T>(payload: T | ApiEnvelope<T> | string): payload is ApiEnvelope<T> {
  return typeof payload === "object" && payload !== null && "success" in payload
}

function apiUrl(path: string): string {
  if (path.startsWith("http://") || path.startsWith("https://")) return path
  const baseUrl = process.env.NEWSROOM_API_BASE_URL ?? DEFAULT_API_BASE_URL
  const suffix = path.startsWith("/") ? path : `/${path}`
  return `${baseUrl.replace(/\/$/, "")}${suffix}`
}

function requestHeaders(extra?: HeadersInit): HeadersInit {
  const headers: Record<string, string> = {
    Accept: "application/json"
  }
  if (extra instanceof Headers) {
    extra.forEach((value, key) => {
      headers[key] = value
    })
  } else if (Array.isArray(extra)) {
    for (const [key, value] of extra) {
      headers[key] = value
    }
  } else if (extra) {
    Object.assign(headers, extra)
  }
  const apiToken = process.env.NEWSROOM_API_TOKEN ?? process.env.NEWS_API_TOKEN
  if (apiToken) {
    headers.Authorization = `Bearer ${apiToken}`
  }
  return headers
}
