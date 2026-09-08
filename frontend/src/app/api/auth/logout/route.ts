import { cookies } from "next/headers"
import { NextRequest } from "next/server"
import { safeApiPost } from "@/lib/api/server"
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session"
import { authFailure, authResponse, isSameOrigin } from "@/lib/auth/portal-server"

export const dynamic = "force-dynamic"

export async function POST(request: NextRequest) {
  if (!isSameOrigin(request)) return authFailure("auth_invalid_origin", 403)
  const token = cookies().get(NEWSROOM_SESSION_COOKIE)?.value
  const result = await safeApiPost(
    "/api/v1/auth/logout",
    {},
    { headers: token ? { "x-newsroom-session": token } : undefined, signal: AbortSignal.timeout(10000) }
  )
  const response = authResponse(result)
  if (result.ok) response.cookies.delete(NEWSROOM_SESSION_COOKIE)
  return response
}
