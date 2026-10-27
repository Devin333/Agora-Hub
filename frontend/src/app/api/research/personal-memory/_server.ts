import { cookies } from "next/headers"
import { NextRequest, NextResponse } from "next/server"
import { safeApiDelete, safeApiPatch, safeApiPut, type SafeApiResult } from "@/lib/api/server"
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session"
import { authFailure, isSameOrigin, readBoundedJson } from "@/lib/auth/portal-server"
import { serverOwnedMutationOriginHeaders } from "@/lib/deployment-profile"
import { desktopFailure, desktopGet, desktopPost } from "@/lib/research/desktop/server-proxy"

const PRIVATE_HEADERS = {
  "Cache-Control": "private, no-store",
  "Pragma": "no-cache",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
}
const MAX_BODY_BYTES = 256 * 1024

type Sanitizer = (value: Record<string, unknown>) => Record<string, unknown> | null

export { desktopFailure, desktopGet, desktopPost }

export const personalMemoryPatch = (path: string, request: NextRequest, sanitize: Sanitizer) =>
  personalMemoryMutation("PATCH", path, request, sanitize)
export const personalMemoryPut = (path: string, request: NextRequest, sanitize: Sanitizer) =>
  personalMemoryMutation("PUT", path, request, sanitize)
export const personalMemoryDelete = (path: string, request: NextRequest, sanitize: Sanitizer) =>
  personalMemoryMutation("DELETE", path, request, sanitize)

async function personalMemoryMutation(
  method: "PATCH" | "PUT" | "DELETE",
  path: string,
  request: NextRequest,
  sanitize: Sanitizer,
) {
  if (!isSameOrigin(request)) return authFailure("auth_invalid_origin", 403)
  const body = await readBoundedJson(request, MAX_BODY_BYTES)
  if (!body) return desktopFailure("invalid_request", 400)
  const sanitized = sanitize(body)
  if (!sanitized) return desktopFailure("invalid_request", 400)
  const token = (await cookies()).get(NEWSROOM_SESSION_COOKIE)?.value
  if (!token) return authFailure("auth_required", 401)
  const headers = {
    "X-Newsroom-Session": token,
    ...serverOwnedMutationOriginHeaders(),
    "Content-Type": "application/json",
  }
  const signal = AbortSignal.any([request.signal, AbortSignal.timeout(120000)])
  const result = method === "PATCH"
    ? await safeApiPatch(path, sanitized, { headers, signal })
    : method === "PUT"
      ? await safeApiPut(path, sanitized, { headers, signal })
      : await safeApiDelete(path, { headers, signal, body: JSON.stringify(sanitized) })
  return personalMemoryResponse(result)
}

function personalMemoryResponse(result: SafeApiResult<unknown>) {
  if (result.ok) {
    return NextResponse.json({ success: true, data: result.data }, { headers: PRIVATE_HEADERS })
  }
  const status = result.status && result.status >= 400 ? result.status : 503
  return NextResponse.json(
    { success: false, error: { code: result.errorCode, message: result.errorMessage, requestId: result.requestId } },
    { status, headers: PRIVATE_HEADERS },
  )
}
