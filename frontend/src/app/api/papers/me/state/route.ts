import { cookies } from "next/headers"
import { NextRequest, NextResponse } from "next/server"
import { safeApiGet } from "@/lib/api/server"
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session"
import { getPaperById, getPublishedPapers } from "@/lib/papers/real-data"
import type { PaperUserState } from "@/lib/papers/types"

export const dynamic = "force-dynamic"

export async function GET(request: NextRequest) {
  const token = (await cookies()).get(NEWSROOM_SESSION_COOKIE)?.value
  const paperIds = request.nextUrl.searchParams.get("paperIds")
  const publicPaperIds = await resolvePublicPaperIds(paperIds)
  if (paperIds && !publicPaperIds.length) {
    return NextResponse.json({ success: true, data: { states: [] } })
  }

  const query = paperIds && publicPaperIds.length ? `?paperIds=${encodeURIComponent(publicPaperIds.join(","))}` : ""
  const result = await safeApiGet(`/api/v1/papers/me/state${query}`, {
    headers: token ? { "x-newsroom-session": token } : undefined,
  })
  return stateResponse(result, publicPaperIds)
}

async function resolvePublicPaperIds(paperIds: string | null): Promise<string[]> {
  if (!paperIds) {
    return (await getPublishedPapers()).map((paper) => paper.id)
  }

  const requestedIds = paperIds
    .split(",")
    .map((paperId) => paperId.trim())
    .filter(Boolean)
  const papers = await Promise.all(requestedIds.map((paperId) => getPaperById(paperId)))
  return papers.flatMap((paper) => (paper ? [paper.id] : []))
}

function stateResponse(result: Awaited<ReturnType<typeof safeApiGet>>, publicPaperIds: string[]) {
  if (result.ok) {
    const publicIdSet = new Set(publicPaperIds)
    const data = result.data as { states?: PaperUserState[] }
    return NextResponse.json({
      success: true,
      data: {
        ...data,
        states: Array.isArray(data.states)
          ? data.states.filter((state) => publicIdSet.has(state.paperId))
          : [],
      },
    })
  }
  return NextResponse.json(
    {
      success: false,
      error: {
        code: result.errorCode,
        message: result.errorMessage,
        requestId: result.requestId,
      },
    },
    { status: result.errorCode === "auth_session_required" ? 401 : 502 }
  )
}
