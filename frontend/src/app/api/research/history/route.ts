import { cookies } from "next/headers"
import { NextRequest, NextResponse } from "next/server"
import { safeApiGet, safeApiPut } from "@/lib/api/server"
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session"
import { authFailure, isSameOrigin } from "@/lib/auth/portal-server"

export const dynamic = "force-dynamic"

const MAX_BODY_BYTES = 4 * 1024 * 1024
const PRIVATE_HEADERS = { "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" }

export async function GET() {
  const token = cookies().get(NEWSROOM_SESSION_COOKIE)?.value
  const result = await safeApiGet("/api/v1/research/history", {
    headers: token ? { "x-newsroom-session": token } : undefined,
    signal: AbortSignal.timeout(10000)
  })
  return historyResponse(result)
}

export async function PUT(request: NextRequest) {
  if (!isSameOrigin(request)) return authFailure("auth_invalid_origin", 403)
  const length = Number(request.headers.get("content-length") ?? "0")
  if (Number.isFinite(length) && length > MAX_BODY_BYTES) return historyFailure("history_payload_too_large", 413)
  const raw = await request.arrayBuffer()
  if (raw.byteLength > MAX_BODY_BYTES) return historyFailure("history_payload_too_large", 413)
  let body: unknown
  try {
    body = JSON.parse(new TextDecoder().decode(raw))
  } catch {
    return historyFailure("invalid_request", 400)
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) return historyFailure("invalid_request", 400)
  const token = cookies().get(NEWSROOM_SESSION_COOKIE)?.value
  const result = await safeApiPut("/api/v1/research/history", body, {
    headers: {
      ...(token ? { "x-newsroom-session": token } : {}),
      "Content-Type": "application/json"
    },
    signal: AbortSignal.timeout(10000)
  })
  return historyResponse(result)
}

function historyResponse(result: { ok: true; data: unknown } | { ok: false; errorCode: string; errorMessage: string; requestId?: string; status?: number }) {
  if (result.ok) return NextResponse.json({ success: true, data: result.data }, { headers: PRIVATE_HEADERS })
  const status = result.status === 401 ? 401 : result.status === 409 ? 409 : result.status === 503 ? 503 : result.status && result.status >= 400 ? result.status : 503
  return NextResponse.json({ success: false, error: { code: result.errorCode, message: result.errorMessage, requestId: result.requestId } }, { status, headers: PRIVATE_HEADERS })
}

function historyFailure(code: string, status: number) {
  return NextResponse.json({ success: false, error: { code } }, { status, headers: PRIVATE_HEADERS })
}
