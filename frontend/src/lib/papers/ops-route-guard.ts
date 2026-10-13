import { cookies } from "next/headers"
import { NextResponse } from "next/server"
import { safeApiGet } from "@/lib/api/server"
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session"

type AuthSessionStatus = {
  session?: {
    user?: {
      role?: string
    } | null
  } | null
}

const PAPER_OPS_ROLES = new Set(["admin", "operator"])

export async function requirePaperOpsSession() {
  const token = (await cookies()).get(NEWSROOM_SESSION_COOKIE)?.value
  if (!token) {
    return forbiddenOpsResponse("auth_session_required", "valid user session required", 401)
  }

  const result = await safeApiGet<AuthSessionStatus>("/api/v1/auth/session", {
    headers: { "x-newsroom-session": token },
  })
  if (!result.ok) {
    const status = result.errorCode === "auth_session_required" ? 401 : 502
    return forbiddenOpsResponse(result.errorCode, result.errorMessage, status)
  }

  const session = result.data.session
  if (!session) {
    return forbiddenOpsResponse("auth_session_required", "valid user session required", 401)
  }

  const role = session.user?.role
  if (!role || !PAPER_OPS_ROLES.has(role)) {
    return forbiddenOpsResponse("paper_ops_forbidden", "paper operations require an admin or operator session", 403)
  }

  return null
}

function forbiddenOpsResponse(code: string, message: string, status: number) {
  return NextResponse.json(
    {
      success: false,
      error: {
        code,
        message,
      },
      request_id: null,
    },
    { status },
  )
}
