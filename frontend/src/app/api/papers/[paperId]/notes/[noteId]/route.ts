import { cookies } from "next/headers"
import { NextRequest, NextResponse } from "next/server"
import { safeApiDelete, safeApiPatch } from "@/lib/api/server"
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session"
import { paperRouteErrorStatus, requirePublicPaper } from "@/lib/papers/public-route-guard"

export const dynamic = "force-dynamic"

export async function PATCH(
  request: NextRequest,
  props: { params: Promise<{ paperId: string; noteId: string }> }
) {
  const params = await props.params;
  const guard = await requirePublicPaper(params.paperId)
  if (!guard.ok) {
    return guard.response
  }

  const token = (await cookies()).get(NEWSROOM_SESSION_COOKIE)?.value
  const body = await request.json().catch(() => ({}))
  const result = await safeApiPatch(
    `/api/v1/papers/${encodeURIComponent(guard.paper.id)}/notes/${encodeURIComponent(params.noteId)}`,
    body,
    {
      headers: token ? { "x-newsroom-session": token } : undefined,
    }
  )
  return noteResponse(result)
}

export async function DELETE(
  _request: NextRequest,
  props: { params: Promise<{ paperId: string; noteId: string }> }
) {
  const params = await props.params;
  const guard = await requirePublicPaper(params.paperId)
  if (!guard.ok) {
    return guard.response
  }

  const token = (await cookies()).get(NEWSROOM_SESSION_COOKIE)?.value
  const result = await safeApiDelete(
    `/api/v1/papers/${encodeURIComponent(guard.paper.id)}/notes/${encodeURIComponent(params.noteId)}`,
    {
      headers: token ? { "x-newsroom-session": token } : undefined,
    }
  )
  return noteResponse(result)
}

function noteResponse(result: Awaited<ReturnType<typeof safeApiPatch>>) {
  if (result.ok) {
    return NextResponse.json({ success: true, data: result.data })
  }
  const status = paperRouteErrorStatus(result.errorCode, {
    invalidCodes: ["paper_reader_note_invalid"],
    notFoundCodes: ["paper_reader_note_not_found"],
  })
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
