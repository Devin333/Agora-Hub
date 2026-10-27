import { cookies } from "next/headers"
import { NextRequest, NextResponse } from "next/server"
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session"
import { authFailure, isSameOrigin } from "@/lib/auth/portal-server"
import { apiBaseUrl } from "@/lib/deployment-profile"

export const dynamic = "force-dynamic"

export async function GET(_request: NextRequest, context: { params: Promise<{ importId: string }> }) {
  const token = (await cookies()).get(NEWSROOM_SESSION_COOKIE)?.value
  if (!token) return authFailure("auth_required", 401)
  const { importId } = (await context.params)
  if (!/^imp_[a-zA-Z0-9_-]{1,128}$/.test(importId)) return authFailure("invalid_request", 400)
  return forward(`${apiBaseUrl()}/api/v1/research/imports/${encodeURIComponent(importId)}`, "GET", token)
}

export async function POST(request: NextRequest, context: { params: Promise<{ importId: string }> }) {
  if (!isSameOrigin(request)) return authFailure("auth_invalid_origin", 403)
  const token = (await cookies()).get(NEWSROOM_SESSION_COOKIE)?.value
  if (!token) return authFailure("auth_required", 401)
  const { importId } = (await context.params)
  if (!/^imp_[a-zA-Z0-9_-]{1,128}$/.test(importId)) return authFailure("invalid_request", 400)
  return forward(`${apiBaseUrl()}/api/v1/research/imports/${encodeURIComponent(importId)}/retry`, "POST", token)
}

async function forward(url: string, method: "GET" | "POST", token: string) {
  const response = await fetch(url, { method, headers: { "X-Newsroom-Session": token }, cache: "no-store", signal: AbortSignal.timeout(120000) }).catch(() => null)
  if (!response) return NextResponse.json({ success: false, error: { code: "research_service_unavailable", message: "转换尚未确认完成，请检查进度。" } }, { status: 503, headers: { "Cache-Control": "no-store" } })
  return new NextResponse(response.body, { status: response.status, headers: { "Content-Type": response.headers.get("content-type") || "application/json", "Cache-Control": "no-store" } })
}
