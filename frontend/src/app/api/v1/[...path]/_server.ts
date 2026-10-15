import { NextRequest, NextResponse } from "next/server"
import { authFailure, isSameOrigin, requestOrigin } from "@/lib/auth/portal-server"
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session"
import {
  desktopCanonicalOrigin,
  isLocalDesktopProfile,
  validateLocalDesktopServiceOrigin,
} from "@/lib/deployment-profile"

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"])
const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
])
const SERVER_OWNED_REQUEST_HEADERS = new Set([
  ...HOP_BY_HOP_HEADERS,
  "accept-encoding",
  "authorization",
  "content-length",
  "cookie",
  "forwarded",
  "host",
  "origin",
  "proxy-connection",
  "via",
  "x-api-client-id",
  "x-http-method-override",
  "x-method-override",
  "x-news-actor",
  "x-news-actor-type",
  "x-news-permissions",
  "x-news-roles",
  "x-newsroom-auth-binding",
  "x-newsroom-session",
  "x-original-url",
  "x-real-ip",
  "x-rewrite-url",
])
const SERVER_OWNED_REQUEST_HEADER_PREFIXES = ["sec-", "x-forwarded-"]
const MAX_PATH_LENGTH = 4096
const MAX_PATH_SEGMENT_LENGTH = 512
const MAX_TARGET_LENGTH = 16 * 1024
const UPSTREAM_DEADLINE_MS = 60_000

type RawRouteContext = { params: Promise<{ path: string[] }> }
type NodeRequestInit = RequestInit & { duplex?: "half" }
type HeadersWithSetCookie = Headers & { getSetCookie?: () => string[] }

export async function forwardLocalDesktopRawApi(
  request: NextRequest,
  context: RawRouteContext,
): Promise<Response> {
  if (!isLocalDesktopProfile()) return rawFailure("raw_api_route_unavailable", 404)
  if (!requestOrigin(request)) return authFailure("auth_invalid_origin", 403)

  const method = request.method.toUpperCase()
  const suppliedOrigin = request.headers.get("origin")
  if (
    request.headers.get("sec-fetch-site") === "cross-site" ||
    (suppliedOrigin !== null && !isSameOrigin(request)) ||
    (MUTATING_METHODS.has(method) && !isSameOrigin(request))
  ) {
    return authFailure("auth_invalid_origin", 403)
  }

  const { path } = await context.params
  const normalizedPath = normalizeRawPath(path)
  if (!normalizedPath) return rawFailure("raw_api_path_invalid", 400)
  if (!supportedRawApiPath(normalizedPath, method)) return rawFailure("raw_api_path_unsupported", 404)

  let apiOrigin: string
  try {
    apiOrigin = validateLocalDesktopServiceOrigin(
      process.env.NEWSROOM_API_BASE_URL ?? DEFAULT_API_BASE_URL,
    )
  } catch {
    return rawFailure("desktop_api_base_invalid", 503)
  }

  const target = new URL(normalizedPath, apiOrigin)
  target.search = request.nextUrl.search
  if (target.toString().length > MAX_TARGET_LENGTH) return rawFailure("raw_api_target_too_long", 414)
  const headers = upstreamRequestHeaders(request, method)
  const body = method === "GET" || method === "HEAD" ? undefined : request.body ?? undefined
  const deadline = upstreamDeadline(request.signal)
  const init: NodeRequestInit = {
    method,
    headers,
    body,
    cache: "no-store",
    redirect: "manual",
    signal: deadline.signal,
  }
  if (body) init.duplex = "half"

  let upstream: Response
  try {
    upstream = await fetch(target, init)
  } catch {
    deadline.dispose()
    if (deadline.timedOut()) return rawFailure("api_upstream_timeout", 504)
    return rawFailure("api_service_unavailable", 503)
  }

  return downstreamResponse(target, upstream, method, deadline)
}

function normalizeRawPath(segments: string[]): string | null {
  if (!Array.isArray(segments) || segments.length === 0) return null
  const encoded: string[] = []
  for (const segment of segments) {
    if (!safePathSegment(segment)) return null
    encoded.push(encodeURIComponent(segment))
  }
  const path = `/api/v1/${encoded.join("/")}`
  return path.length <= MAX_PATH_LENGTH ? path : null
}

function safePathSegment(value: string): boolean {
  if (!value || value.length > MAX_PATH_SEGMENT_LENGTH) return false
  let decoded = value
  for (let attempt = 0; attempt < 3; attempt += 1) {
    if (unsafeDecodedSegment(decoded)) return false
    let next: string
    try {
      next = decodeURIComponent(decoded)
    } catch {
      return false
    }
    if (next === decoded) return true
    decoded = next
  }
  // Only a fixed point is safe. Residual encodings at the budget limit must
  // never reach a downstream decoder with a different normalization policy.
  return false
}

function supportedRawApiPath(path: string, method: string): boolean {
  if (path === "/api/v1/auth/session" || path === "/api/v1/auth/methods") {
    return method === "GET" || method === "HEAD"
  }
  return path === "/api/v1/research" || path.startsWith("/api/v1/research/")
}

function unsafeDecodedSegment(value: string): boolean {
  return value === "." || value === ".." || /[\\/\x00-\x1F\x7F]/.test(value)
}

function upstreamRequestHeaders(request: NextRequest, method: string): Headers {
  const headers = new Headers()
  const connectionHeaders = connectionHeaderNames(request.headers)
  request.headers.forEach((value, name) => {
    const normalized = name.toLowerCase()
    if (
      SERVER_OWNED_REQUEST_HEADERS.has(normalized) ||
      connectionHeaders.has(normalized) ||
      SERVER_OWNED_REQUEST_HEADER_PREFIXES.some(prefix => normalized.startsWith(prefix))
    ) {
      return
    }
    headers.append(name, value)
  })

  const session = request.cookies.get(NEWSROOM_SESSION_COOKIE)?.value?.trim()
  if (session) headers.set("X-Newsroom-Session", session)
  if (MUTATING_METHODS.has(method)) headers.set("Origin", desktopCanonicalOrigin())
  headers.set("Accept-Encoding", "identity")
  return headers
}

function downstreamResponse(
  target: URL,
  upstream: Response,
  method: string,
  deadline: ReturnType<typeof upstreamDeadline>,
): Response {
  const headers = downstreamHeaders(upstream.headers)
  if (upstream.status >= 300 && upstream.status < 400) {
    const location = headers.get("location")
    if (location) {
      const rewritten = rewriteRedirectLocation(target, location, method)
      if (!rewritten) {
        deadline.cancel()
        void upstream.body?.cancel().catch(() => undefined)
        return rawFailure("api_redirect_forbidden", 502)
      }
      headers.set("Location", rewritten)
    }
  }
  const bodyForbidden = method === "HEAD" || [204, 205, 304].includes(upstream.status)
  if (bodyForbidden || !upstream.body) {
    deadline.dispose()
    if (bodyForbidden) void upstream.body?.cancel().catch(() => undefined)
  }
  return new Response(bodyForbidden || !upstream.body ? null : deadlineStream(upstream.body, deadline), {
    status: upstream.status,
    statusText: upstream.statusText,
    headers,
  })
}

function downstreamHeaders(source: Headers): Headers {
  const headers = new Headers()
  const connectionHeaders = connectionHeaderNames(source)
  source.forEach((value, name) => {
    const normalized = name.toLowerCase()
    if (!HOP_BY_HOP_HEADERS.has(normalized) && !connectionHeaders.has(normalized)) {
      headers.append(name, value)
    }
  })
  const setCookies = (source as HeadersWithSetCookie).getSetCookie?.() ?? []
  if (setCookies.length > 0) {
    headers.delete("set-cookie")
    for (const cookie of setCookies) headers.append("set-cookie", cookie)
  }
  return headers
}

function connectionHeaderNames(headers: Headers): Set<string> {
  return new Set(
    (headers.get("connection") ?? "")
      .split(",")
      .map(value => value.trim().toLowerCase())
      .filter(Boolean),
  )
}

function rewriteRedirectLocation(target: URL, location: string, method: string): string | null {
  // URL resolves dot segments before exposing pathname. Validate the original
  // path first so normalization cannot hide traversal in a Location header.
  const originalPath = location.split(/[?#]/, 1)[0]
    .replace(/^(?:[a-z][a-z0-9+.-]*:)?\/\/[^/]*/i, "")
  try {
    if (originalPath.split("/").filter(Boolean).some(segment => !safePathSegment(decodeURIComponent(segment)))) {
      return null
    }
  } catch {
    return null
  }
  let resolved: URL
  try {
    resolved = new URL(location, target)
  } catch {
    return null
  }
  if (resolved.origin !== target.origin || resolved.username || resolved.password) return null
  if (!resolved.pathname.startsWith("/api/v1/")) return null
  let segments: string[]
  try {
    segments = resolved.pathname.slice("/api/v1/".length).split("/").map(segment => decodeURIComponent(segment))
  } catch {
    return null
  }
  const path = normalizeRawPath(segments)
  if (!path || !supportedRawApiPath(path, method)) return null
  const rewritten = `${path}${resolved.search}${resolved.hash}`
  return rewritten.length <= MAX_TARGET_LENGTH ? rewritten : null
}

function upstreamDeadline(clientSignal: AbortSignal) {
  const controller = new AbortController()
  let expired = false
  const abortFromClient = () => controller.abort(clientSignal.reason)
  const timer = setTimeout(() => {
    expired = true
    controller.abort(new DOMException("Upstream deadline exceeded", "TimeoutError"))
  }, UPSTREAM_DEADLINE_MS)
  clientSignal.addEventListener("abort", abortFromClient, { once: true })
  if (clientSignal.aborted) abortFromClient()
  const dispose = () => {
    clearTimeout(timer)
    clientSignal.removeEventListener("abort", abortFromClient)
  }
  return {
    signal: controller.signal,
    timedOut: () => expired,
    dispose,
    cancel: () => {
      controller.abort(new DOMException("Response cancelled", "AbortError"))
      dispose()
    },
  }
}

function deadlineStream(source: ReadableStream<Uint8Array>, deadline: ReturnType<typeof upstreamDeadline>) {
  const reader = source.getReader()
  let ended = false
  let abort: () => void
  const finish = () => {
    ended = true
    deadline.signal.removeEventListener("abort", abort)
    deadline.dispose()
  }
  return new ReadableStream<Uint8Array>({
    start(controller) {
      abort = () => {
        if (ended) return
        finish()
        controller.error(deadline.signal.reason)
        void reader.cancel(deadline.signal.reason).catch(() => undefined)
      }
      deadline.signal.addEventListener("abort", abort, { once: true })
      if (deadline.signal.aborted) abort()
    },
    async pull(controller) {
      try {
        const result = await reader.read()
        if (ended) return
        if (result.done) {
          finish()
          controller.close()
        } else {
          controller.enqueue(result.value)
        }
      } catch (error) {
        if (ended) return
        finish()
        controller.error(error)
      }
    },
    async cancel(reason) {
      if (ended) return
      finish()
      deadline.cancel()
      await reader.cancel(reason)
    },
  })
}

function rawFailure(code: string, status: number): NextResponse {
  return NextResponse.json(
    { success: false, error: { code } },
    {
      status,
      headers: {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
      },
    },
  )
}
