import { cookies } from "next/headers"
import { NextRequest, NextResponse } from "next/server"
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session"
import { authFailure, isSameOrigin } from "@/lib/auth/portal-server"

export const dynamic = "force-dynamic"
const MAX_BODY_BYTES = 50 * 1024 * 1024
const backend = process.env.NEWSROOM_API_BASE_URL ?? process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000"

export async function POST(request: NextRequest) {
  if (!isSameOrigin(request)) return authFailure("auth_invalid_origin", 403)
  const token = cookies().get(NEWSROOM_SESSION_COOKIE)?.value
  if (!token) return authFailure("auth_required", 401)
  const length = Number(request.headers.get("content-length") ?? "0")
  if (Number.isFinite(length) && length > MAX_BODY_BYTES) return authFailure("pdf_too_large", 413)
  const body = await request.arrayBuffer()
  if (body.byteLength > MAX_BODY_BYTES) return authFailure("pdf_too_large", 413)
  const response = await fetch(`${backend}/api/v1/research/imports`, {
    method: "POST",
    headers: { "Content-Type": "application/pdf", "X-Filename": request.headers.get("x-filename") || "paper.pdf", "X-Newsroom-Session": token },
    body,
    cache: "no-store",
    signal: AbortSignal.timeout(120000),
  }).catch(() => null)
  return proxy(response)
}

function proxy(response: Response | null) {
  if (!response) return NextResponse.json({ success: false, error: { code: "research_service_unavailable", message: "Research service is unavailable" } }, { status: 503 })
  return new NextResponse(response.body, { status: response.status, headers: { "Content-Type": response.headers.get("content-type") || "application/json", "Cache-Control": "no-store" } })
}
