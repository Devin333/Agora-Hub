import { cookies } from "next/headers"
import { NextResponse } from "next/server"
import { safeApiDelete, safeApiGet } from "@/lib/api/server"
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session"
import { paperRouteErrorStatus, requirePublicPaper } from "@/lib/papers/public-route-guard"

export const dynamic = "force-dynamic"

export async function GET(_request: Request, props: { params: Promise<{ paperId: string }> }) {
  const params = await props.params;
  const guard = await requirePublicPaper(params.paperId)
  if (!guard.ok) {
    return guard.response
  }

  const token = (await cookies()).get(NEWSROOM_SESSION_COOKIE)?.value
  const result = await safeApiGet(`/api/v1/papers/${encodeURIComponent(guard.paper.id)}/reader/materials`, {
    headers: token ? { "x-newsroom-session": token } : undefined,
  })
  return readerMaterialsResponse(result)
}

export async function DELETE(_request: Request, props: { params: Promise<{ paperId: string }> }) {
  const params = await props.params;
  const guard = await requirePublicPaper(params.paperId)
  if (!guard.ok) {
    return guard.response
  }

  const token = (await cookies()).get(NEWSROOM_SESSION_COOKIE)?.value
  const result = await safeApiDelete(`/api/v1/papers/${encodeURIComponent(guard.paper.id)}/reader/materials`, {
    headers: token ? { "x-newsroom-session": token } : undefined,
  })
  return readerMaterialsResponse(result)
}

function readerMaterialsResponse(result: Awaited<ReturnType<typeof safeApiGet>>) {
  if (result.ok) {
    return NextResponse.json({ success: true, data: result.data })
  }
  const status = paperRouteErrorStatus(result.errorCode)
  return NextResponse.json(
    {
      success: false,
      error: {
        code: result.errorCode,
        message: result.errorMessage,
        requestId: result.requestId,
      },
    },
    { status }
  )
}
