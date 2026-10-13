import { NextRequest, NextResponse } from "next/server"
import { safeApiGet } from "@/lib/api/server"
import { requirePublicPaper } from "@/lib/papers/public-route-guard"

export const dynamic = "force-dynamic"

export async function GET(_request: NextRequest, props: { params: Promise<{ paperId: string }> }) {
  const params = await props.params;
  const guard = await requirePublicPaper(params.paperId)
  if (!guard.ok) {
    return guard.response
  }

  const result = await safeApiGet(`/api/v1/papers/${encodeURIComponent(guard.paper.id)}/related`)
  if (result.ok) {
    return NextResponse.json({ success: true, data: result.data })
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
    { status: result.errorCode === "paper_not_found" ? 404 : 502 }
  )
}
