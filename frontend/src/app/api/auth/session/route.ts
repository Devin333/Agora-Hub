import { cookies } from "next/headers"
import { safeApiGet } from "@/lib/api/server"
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session"
import { authFailure, authResponse } from "@/lib/auth/portal-server"
import { attachFrontendRuntimeIdentity } from "@/lib/runtime-identity"

export const dynamic = "force-dynamic"

export async function GET() {
  const token = (await cookies()).get(NEWSROOM_SESSION_COOKIE)?.value
  const result = await safeApiGet("/api/v1/auth/session", {
    headers: token ? { "x-newsroom-session": token } : undefined,
    signal: AbortSignal.timeout(10000),
  })
  let response
  if (result.ok) {
    try {
      const data = result.data && typeof result.data === "object"
        ? result.data as Record<string, unknown>
        : {}
      response = authResponse(
        { ...result, data: attachFrontendRuntimeIdentity(data) },
        { issueSession: false },
      )
    } catch {
      return authFailure("desktop_runtime_identity_invalid", 503)
    }
  } else {
    response = authResponse(result, { issueSession: false })
  }
  if (result.ok) {
    const data = result.data as { session?: unknown } | undefined
    if (token && !data?.session) {
      response.cookies.delete(NEWSROOM_SESSION_COOKIE)
    }
  }
  return response
}
