import { cookies } from "next/headers"
import { NextRequest, NextResponse } from "next/server"
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session"
import { authFailure, isSameOrigin } from "@/lib/auth/portal-server"

export const dynamic = "force-dynamic"
const backend = process.env.NEWSROOM_API_BASE_URL ?? process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000"

export async function GET(_request: NextRequest, context: { params: { importId: string } }) {
  const token = cookies().get(NEWSROOM_SESSION_COOKIE)?.value
  if (!token) return authFailure("auth_required", 401)
  const { importId } = context.params
  return forward(`${backend}/api/v1/research/imports/${encodeURIComponent(importId)}`, "GET", token)
}

export async function POST(request: NextRequest, context: { params: { importId: string } }) {
  if (!isSameOrigin(request)) return authFailure("auth_invalid_origin", 403)
  const token = cookies().get(NEWSROOM_SESSION_COOKIE)?.value
  if (!token) return authFailure("auth_required", 401)
  const { importId } = context.params
  return forward(`${backend}/api/v1/research/imports/${encodeURIComponent(importId)}/retry`, "POST", token)
}

async function forward(url: string, method: "GET" | "POST", token: string) {
  const response = await fetch(url, { method, headers: { "X-Newsroom-Session": token }, cache: "no-store", signal: AbortSignal.timeout(120000) }).catch(() => null)
  if (!response) return NextResponse.json({ success: false, error: { code: "research_service_unavailable", message: "Research service is unavailable" } }, { status: 503 })
  return new NextResponse(response.body, { status: response.status, headers: { "Content-Type": response.headers.get("content-type") || "application/json", "Cache-Control": "no-store" } })
}
