import { NextRequest } from "next/server"
import { authFailure, proxyAuthPost } from "@/lib/auth/portal-server"

export const dynamic = "force-dynamic"
export async function POST(request: NextRequest, { params }: { params: { challengeId: string } }) {
  if (!/^[A-Za-z0-9_-]{16,128}$/.test(params.challengeId)) return authFailure("auth_challenge_invalid")
  return proxyAuthPost(request, `/api/v1/auth/otp/challenges/${params.challengeId}/verify`, (body) =>
    typeof body.code === "string" && /^\d{6}$/.test(body.code) ? { code: body.code } : null)
}
