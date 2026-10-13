import { cookies } from "next/headers"
import { NextRequest, NextResponse } from "next/server"
import { safeApiPost } from "@/lib/api/server"
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session"
import { paperRouteErrorStatus, requirePublicPaper } from "@/lib/papers/public-route-guard"

export const dynamic = "force-dynamic"

export async function POST(request: NextRequest, props: { params: Promise<{ paperId: string }> }) {
  const params = await props.params;
  const guard = await requirePublicPaper(params.paperId)
  if (!guard.ok) {
    return guard.response
  }

  const token = (await cookies()).get(NEWSROOM_SESSION_COOKIE)?.value
  const body = await request.json().catch(() => ({}))
  const result = await safeApiPost(`/api/v1/papers/${encodeURIComponent(guard.paper.id)}/reader/events`, body, {
    headers: token ? { "x-newsroom-session": token } : undefined,
  })
  return readerInteractionResponse(result)
}

function readerInteractionResponse(result: Awaited<ReturnType<typeof safeApiPost>>) {
  if (result.ok) {
    return NextResponse.json({ success: true, data: result.data })
  }
  const status = paperRouteErrorStatus(result.errorCode, { invalidCodes: ["paper_reader_event_invalid"] })
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
