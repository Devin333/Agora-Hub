import { cookies } from "next/headers"
import { NextRequest } from "next/server"
import { safeApiPost } from "@/lib/api/server"
import { authFailure, authResponse, isSameOrigin, readBoundedJson } from "@/lib/auth/portal-server"
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session"
import { isHostedProfile, serverOwnedMutationOriginHeaders } from "@/lib/deployment-profile"

export const dynamic = "force-dynamic"

const PASSWORD_FIELDS = ["currentPassword", "newPassword"]

// Changing the password revokes every session of the account, including this
// one, so the BFF must replace the cookie with the session the API returns.
export async function POST(request: NextRequest) {
  if (!isHostedProfile()) return authFailure("auth_route_unavailable", 404)
  if (!isSameOrigin(request)) return authFailure("auth_invalid_origin", 403)
  const token = (await cookies()).get(NEWSROOM_SESSION_COOKIE)?.value?.trim()
  if (!token) return authFailure("auth_required", 401)

  const body = await readBoundedJson(request)
  if (
    !body ||
    Object.keys(body).some(key => !PASSWORD_FIELDS.includes(key)) ||
    typeof body.currentPassword !== "string" ||
    typeof body.newPassword !== "string" ||
    body.currentPassword.length < 1 ||
    body.currentPassword.length > 256 ||
    body.newPassword.length < 8 ||
    body.newPassword.length > 256
  ) {
    return authFailure("auth_invalid_request")
  }

  const result = await safeApiPost(
    "/api/v1/account/password",
    { currentPassword: body.currentPassword, newPassword: body.newPassword },
    {
      headers: { "X-Newsroom-Session": token, ...serverOwnedMutationOriginHeaders() },
      signal: AbortSignal.timeout(15000),
    },
  )
  if (!result.ok) {
    if (result.errorCode === "auth_invalid_credentials") return authFailure("auth_invalid_credentials", 400)
    if (result.errorCode === "account_invalid_request") return authFailure("auth_invalid_request", 400)
    if (result.errorCode === "auth_required") return authFailure("auth_required", 401)
  }
  return authResponse(result)
}
