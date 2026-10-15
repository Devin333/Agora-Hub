import { cookies } from "next/headers"
import { NextRequest } from "next/server"
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session"
import { authFailure, authResponse, isSameOrigin, publicAuthPost } from "@/lib/auth/portal-server"

export const dynamic = "force-dynamic"

export async function POST(request: NextRequest) {
  if (!isSameOrigin(request)) return authFailure("auth_invalid_origin", 403)
  const token = (await cookies()).get(NEWSROOM_SESSION_COOKIE)?.value
  const result = await publicAuthPost(
    "/api/v1/auth/logout",
    {},
    { headers: token ? { "x-newsroom-session": token } : undefined, signal: AbortSignal.timeout(10000) }
  )
  const response = authResponse(result)
  if (result.ok) response.cookies.delete(NEWSROOM_SESSION_COOKIE)
  return response
}
