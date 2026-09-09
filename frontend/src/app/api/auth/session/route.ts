import { cookies } from "next/headers"
import { NextResponse } from "next/server"
import { safeApiGet } from "@/lib/api/server"
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session"
import { authResponse } from "@/lib/auth/portal-server"

export const dynamic = "force-dynamic"

export async function GET() {
  const token = cookies().get(NEWSROOM_SESSION_COOKIE)?.value
  if (!token) return NextResponse.json({ success: true, data: { session: null } }, { headers: { "Cache-Control": "no-store" } })
  const result = await safeApiGet("/api/v1/auth/session", {
    headers: token ? { "x-newsroom-session": token } : undefined,
    signal: AbortSignal.timeout(10000),
  })
  const response = authResponse(result, { issueSession: false })
  if (result.ok) {
    const data = result.data as { session?: unknown } | undefined
    if (token && !data?.session) {
      response.cookies.delete(NEWSROOM_SESSION_COOKIE)
    }
  }
  return response
}
